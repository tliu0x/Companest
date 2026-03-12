"""
Companest Gateway WebSocket Client

Async WebSocket client implementing the Gateway JSON-RPC protocol.

Protocol: JSON frames over WebSocket
- Request:  {type: "req", id, method, params}
- Response: {type: "res", id, ok, payload|error}
- Event:    {type: "event", event, payload, seq}

Usage:
    client = GatewayClient("ws://localhost:19000", auth_token="secret")
    await client.connect()

    result = await client.agent_task("Write a hello world in Python")
    print(result)

    await client.disconnect()
"""

import json
import uuid
import asyncio
import logging
from typing import Any, Dict, List, Optional, Callable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .exceptions import (
    GatewayError,
    GatewayConnectionError,
    GatewayAuthError,
)

logger = logging.getLogger(__name__)


class ClientState(str, Enum):
    """WebSocket client connection state"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


@dataclass
class GatewayEvent:
    """Represents an event received from the gateway"""
    event: str
    payload: Dict[str, Any]
    seq: int
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PendingRequest:
    """Tracks a pending request awaiting response"""
    id: str
    method: str
    future: asyncio.Future
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeout: float = 60.0


class GatewayClient:
    """
    Async WebSocket client for the Gateway JSON-RPC protocol.

    Handles connection lifecycle, request/response correlation,
    authentication, and automatic reconnection.

    Example:
        async with GatewayClient("ws://localhost:19000", auth_token="tk") as client:
            result = await client.agent_task("Analyze this code")
            print(result)
    """

    def __init__(
        self,
        ws_url: str,
        auth_token: Optional[str] = None,
        auth_password: Optional[str] = None,
        role: str = "controller",
        scopes: Optional[List[str]] = None,
        protocol_version: str = "1.0",
        connect_timeout: float = 10.0,
        request_timeout: float = 60.0,
        reconnect: bool = True,
        max_reconnect_attempts: int = 10,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        on_event: Optional[Callable[[GatewayEvent], None]] = None,
        on_inbound_request: Optional[Callable[[dict], None]] = None,
    ):
        self.ws_url = ws_url
        self.auth_token = auth_token
        self.auth_password = auth_password
        self.role = role
        self.scopes = scopes or ["operator.admin", "operator.read", "operator.write"]
        self.protocol_version = protocol_version
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.reconnect = reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.on_event = on_event
        self.on_inbound_request = on_inbound_request

        # Internal state
        self._ws = None
        self._state = ClientState.DISCONNECTED
        self._pending: Dict[str, PendingRequest] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._consecutive_failures = 0
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._seq_counter = 0

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ClientState.CONNECTED

    async def connect(self) -> None:
        """
        Connect to the gateway and authenticate.

        Raises:
            GatewayConnectionError: If connection fails
            GatewayAuthError: If auth handshake fails
        """
        if self._state == ClientState.CONNECTED:
            return

        self._state = ClientState.CONNECTING

        try:
            import websockets
        except ImportError:
            raise GatewayError(
                "websockets package required. Install with: pip install websockets"
            )

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.ws_url),
                timeout=self.connect_timeout,
            )
        except asyncio.TimeoutError:
            self._state = ClientState.DISCONNECTED
            raise GatewayConnectionError(
                f"Connection timed out after {self.connect_timeout}s",
                details={"url": self.ws_url},
            )
        except Exception as e:
            self._state = ClientState.DISCONNECTED
            raise GatewayConnectionError(
                f"Failed to connect to {self.ws_url}: {e}",
                details={"url": self.ws_url},
            )

        # Authenticate
        self._state = ClientState.AUTHENTICATING
        try:
            await self._authenticate()
        except Exception:
            await self._close_ws()
            self._state = ClientState.DISCONNECTED
            raise

        self._state = ClientState.CONNECTED
        self._consecutive_failures = 0

        # Start receive loop
        self._recv_task = asyncio.create_task(self._receive_loop())

        logger.info(f"Connected to gateway at {self.ws_url}")

    async def disconnect(self) -> None:
        """Cleanly disconnect from the gateway."""
        self._state = ClientState.CLOSED

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        await self._close_ws()

        # Cancel all pending requests
        for req in self._pending.values():
            if not req.future.done():
                req.future.set_exception(
                    GatewayConnectionError("Client disconnected")
                )
        self._pending.clear()

        logger.info(f"Disconnected from {self.ws_url}")

    async def send_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Send a request frame and await the response.

        Args:
            method: The RPC method name
            params: Optional parameters
            timeout: Override default request timeout

        Returns:
            Response payload

        Raises:
            GatewayError: On protocol error
            ConnectionError: If not connected
        """
        if not self.is_connected:
            raise GatewayConnectionError("Not connected to gateway")

        request_id = str(uuid.uuid4())
        timeout = timeout or self.request_timeout

        frame = {
            "type": "req",
            "id": request_id,
            "method": method,
        }
        if params:
            frame["params"] = params

        # Create future for response
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = PendingRequest(
            id=request_id,
            method=method,
            future=future,
            timeout=timeout,
        )

        try:
            await self._send_frame(frame)
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise GatewayError(
                f"Request timed out after {timeout}s",
                details={"method": method, "id": request_id},
            )
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def agent_task(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        session_id: str = "agent:main:main",
    ) -> str:
        """
        Submit a task via the 'agent' method and return the final result.

        Gateway protocol:
        1. Send agent request -> get {runId, status: "accepted"}
        2. Stream events: {event: "agent", payload: {runId, stream, data}}
           - stream "lifecycle" + data.phase "start"/"end"
           - stream "assistant" + data.text (cumulative) / data.delta
        3. Final res frame: {runId, status: "ok", result: {payloads: [{text}]}}

        Args:
            task: The task/prompt to execute
            context: Optional context dict
            timeout: Override default timeout
            session_id: Gateway session key (default: "agent:main:main")

        Returns:
            The agent's response text
        """
        timeout = timeout or self.request_timeout

        params = {
            "message": task,
            "idempotencyKey": str(uuid.uuid4()),
            "sessionId": session_id,
        }
        if context:
            params["context"] = context

        logger.info(f"Sending agent task (session={session_id})...")

        # Phase 1: Send request and get acceptance
        response = await self.send_request(
            "agent",
            params=params,
            timeout=30.0,
        )

        run_id = None
        if isinstance(response, dict):
            run_id = response.get("runId")
            # Check if result is already included (fast path)
            result_obj = response.get("result")
            if isinstance(result_obj, dict):
                payloads = result_obj.get("payloads", [])
                if payloads:
                    return payloads[0].get("text", "")

        if not run_id:
            return json.dumps(response) if isinstance(response, dict) else str(response)

        logger.info(f"Agent task accepted (runId={run_id[:8]}...), waiting for completion...")

        # Phase 2: Listen for streaming events until lifecycle "end"
        last_text = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            try:
                remaining = deadline - loop.time()
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=min(remaining, 5.0)
                )

                payload = event.payload or {}
                event_run_id = payload.get("runId", "")

                if event_run_id and event_run_id != run_id:
                    continue

                stream = payload.get("stream", "")
                data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

                if event.event == "agent":
                    if stream == "assistant":
                        # data.text is cumulative, data.delta is incremental
                        text = data.get("text", "")
                        if text:
                            last_text = text
                    elif stream == "lifecycle":
                        phase = data.get("phase", "")
                        if phase == "end":
                            logger.info(f"Agent completed ({len(last_text)} chars)")
                            break
                    elif stream == "error":
                        error_msg = data.get("message", data.get("error", "Agent error"))
                        raise GatewayError(f"Agent error: {error_msg}")

            except asyncio.TimeoutError:
                continue

        if last_text:
            return last_text

        return f"Task accepted (runId={run_id}) but no result received within timeout"

    async def sessions_send(
        self,
        session_id: str,
        message: str,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a message to an existing session."""
        return await self.send_request(
            "sessions.send",
            params={"session_id": session_id, "message": message},
            timeout=timeout,
        )

    async def sessions_list(self, timeout: Optional[float] = None) -> List[Dict]:
        """List active sessions."""
        result = await self.send_request("sessions.list", timeout=timeout)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("sessions", [])
        return []

    async def get_config(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Get server configuration."""
        result = await self.send_request("config.get", timeout=timeout)
        return result if isinstance(result, dict) else {}

    async def health_check(self, timeout: Optional[float] = None) -> bool:
        """
        Perform a health check (ping) on the connection.

        Returns:
            True if healthy, False otherwise
        """
        try:
            await self.send_request(
                "ping",
                timeout=timeout or 5.0,
            )
            return True
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Internal Protocol Handling
    # -------------------------------------------------------------------------

    async def _authenticate(self) -> None:
        """
        Perform the authentication handshake.

        Supports two protocols:
        - Challenge-response gateway: receives challenge first
        - Simple gateway: direct connect (client sends first)
        """
        # Try to receive a challenge (some gateways send one immediately)
        nonce = None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=3.0)
            first_frame = json.loads(raw)

            if (first_frame.get("type") == "event"
                    and first_frame.get("event") == "connect.challenge"):
                nonce = first_frame.get("payload", {}).get("nonce")
                logger.debug(f"Received connect challenge (nonce={nonce[:8]}...)")
            else:
                # Not a challenge  treat as unexpected response
                self._handle_auth_response(first_frame)
                return
        except asyncio.TimeoutError:
            # No challenge received  use simple connect flow
            logger.debug("No challenge received, using simple connect flow")

        # Build connect request
        if nonce:
            # Challenge-response flow
            auth_params: Dict[str, Any] = {
                "minProtocol": 3,
                "maxProtocol": 3,
                "role": "operator",
                "scopes": self.scopes,
                "client": {
                    "id": "cli",
                    "version": "0.1.0",
                    "platform": "python",
                    "mode": "cli",
                },
            }
            if self.auth_token:
                auth_params["auth"] = {"token": self.auth_token}
        else:
            # Simple connect flow (our master-gateway)
            auth_params = {
                "protocol": self.protocol_version,
                "role": self.role,
                "scopes": self.scopes,
            }
            if self.auth_token:
                auth_params["auth"] = self.auth_token
            if self.auth_password:
                auth_params["password"] = self.auth_password

        frame = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": auth_params,
        }

        await self._send_frame(frame)

        # Wait for connect response
        try:
            raw = await asyncio.wait_for(
                self._ws.recv(),
                timeout=self.connect_timeout,
            )
            response = json.loads(raw)
        except asyncio.TimeoutError:
            raise GatewayAuthError("Authentication timed out")
        except Exception as e:
            raise GatewayAuthError(f"Failed to receive auth response: {e}")

        self._handle_auth_response(response)

    def _handle_auth_response(self, response: Dict[str, Any]) -> None:
        """Validate the authentication response frame."""
        if response.get("type") != "res":
            raise GatewayAuthError(
                f"Unexpected auth response type: {response.get('type')}",
                details=response,
            )

        if not response.get("ok", False):
            error = response.get("error", {})
            msg = error.get("message", "Authentication failed") if isinstance(error, dict) else str(error)
            raise GatewayAuthError(msg, details=response)

        logger.debug("Authentication successful")

    async def _send_frame(self, frame: Dict[str, Any]) -> None:
        """Send a JSON frame over WebSocket."""
        if not self._ws:
            raise GatewayConnectionError("WebSocket not connected")
        try:
            data = json.dumps(frame)
            await self._ws.send(data)
            logger.debug(f"Sent frame: {frame.get('method', frame.get('type'))}")
        except Exception as e:
            raise GatewayConnectionError(f"Failed to send frame: {e}")

    async def _receive_loop(self) -> None:
        """Background loop for receiving and dispatching frames."""
        try:
            async for raw_message in self._ws:
                try:
                    frame = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"Received invalid JSON: {raw_message[:200]}")
                    continue

                frame_type = frame.get("type")

                if frame_type == "res":
                    self._handle_response(frame)
                elif frame_type == "req":
                    self._handle_inbound_request(frame)
                elif frame_type == "event":
                    self._handle_event(frame)
                else:
                    logger.debug(f"Unknown frame type: {frame_type}")

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Receive loop error: {e}")
            if self._state == ClientState.CONNECTED and self.reconnect:
                self._state = ClientState.RECONNECTING
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _handle_response(self, frame: Dict[str, Any]) -> None:
        """Handle a response frame by resolving the pending future."""
        request_id = frame.get("id")
        if not request_id:
            logger.warning("Response frame missing id")
            return

        pending = self._pending.pop(request_id, None)
        if not pending:
            logger.debug(f"No pending request for id: {request_id}")
            return

        if pending.future.done():
            return

        if frame.get("ok", False):
            pending.future.set_result(frame.get("payload"))
        else:
            error = frame.get("error", {})
            msg = error.get("message", "Request failed") if isinstance(error, dict) else str(error)
            pending.future.set_exception(GatewayError(msg, details=frame))

    def _handle_event(self, frame: Dict[str, Any]) -> None:
        """Handle an event frame."""
        event = GatewayEvent(
            event=frame.get("event", "unknown"),
            payload=frame.get("payload", {}),
            seq=frame.get("seq", 0),
        )

        # Call user callback if registered
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")

        # Queue for async consumers
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping event")

    def _handle_inbound_request(self, frame: Dict[str, Any]) -> None:
        """Handle an inbound request frame from the remote end."""
        if self.on_inbound_request:
            try:
                self.on_inbound_request(frame)
            except Exception as e:
                logger.error(f"Inbound request callback error: {e}")
        else:
            logger.debug(f"No inbound request handler for: {frame.get('method')}")

    async def send_response(
        self,
        request_id: str,
        ok: bool,
        payload: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send a response frame back to the remote end.

        Args:
            request_id: The id from the inbound request
            ok: Whether the request succeeded
            payload: Response payload (when ok=True)
            error: Error details (when ok=False)
        """
        frame = {
            "type": "res",
            "id": request_id,
            "ok": ok,
        }
        if ok and payload is not None:
            frame["payload"] = payload
        if not ok and error is not None:
            frame["error"] = error

        await self._send_frame(frame)

    async def send_notification(
        self,
        chat_id: str,
        channel: str,
        user_id: str,
        message: str,
    ) -> None:
        """
        Send a notification frame to the gateway (push delivery).

        Used for scheduled task results and other async notifications
        that aren't tied to a specific request/response cycle.

        Args:
            chat_id: Target chat/channel ID
            channel: Messaging platform ("telegram", etc.)
            user_id: Originating user ID
            message: Notification content
        """
        frame = {
            "type": "notification",
            "payload": {
                "chat_id": chat_id,
                "channel": channel,
                "user_id": user_id,
                "message": message,
            },
        }
        await self._send_frame(frame)
        logger.debug(f"Sent notification to {channel}/{chat_id}")

    async def send_progress(self, request_id: str, message: str) -> None:
        """
        Send a progress frame to the remote end.

        Progress frames are fire-and-forget updates sent during request
        processing (e.g. routing decisions, model selection, cascade steps).

        Args:
            request_id: The id of the in-flight request
            message: Human-readable progress message
        """
        frame = {
            "type": "progress",
            "id": request_id,
            "payload": {"message": message},
        }
        await self._send_frame(frame)

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        attempt = 0
        while (
            self._state == ClientState.RECONNECTING
            and attempt < self.max_reconnect_attempts
        ):
            delay = min(
                self.reconnect_base_delay * (2 ** attempt),
                self.reconnect_max_delay,
            )
            logger.info(
                f"Reconnecting in {delay:.1f}s (attempt {attempt + 1}/{self.max_reconnect_attempts})"
            )
            await asyncio.sleep(delay)

            try:
                import websockets
                self._ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url),
                    timeout=self.connect_timeout,
                )
                await self._authenticate()
                self._state = ClientState.CONNECTED
                self._consecutive_failures = 0
                self._recv_task = asyncio.create_task(self._receive_loop())
                logger.info("Reconnected successfully")
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                attempt += 1
                self._consecutive_failures += 1
                logger.warning(f"Reconnect attempt {attempt} failed: {e}")

        if self._state == ClientState.RECONNECTING:
            logger.error("Max reconnection attempts reached")
            self._state = ClientState.DISCONNECTED
            # Fail all pending requests
            for req in self._pending.values():
                if not req.future.done():
                    req.future.set_exception(
                        GatewayConnectionError("Reconnection failed")
                    )
            self._pending.clear()

    async def _close_ws(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def events(self) -> AsyncIterator[GatewayEvent]:
        """Async iterator for receiving events."""
        while self._state in (ClientState.CONNECTED, ClientState.RECONNECTING):
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    # -------------------------------------------------------------------------
    # Context Manager
    # -------------------------------------------------------------------------

    async def __aenter__(self) -> "GatewayClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    def __repr__(self) -> str:
        return f"GatewayClient(url={self.ws_url!r}, state={self._state.value})"
