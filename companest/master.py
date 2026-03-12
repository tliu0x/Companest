"""
Companest Master Connection

Connects Companest to a master gateway as a controller.
The master is the user-facing gateway (Telegram, web chat, etc.).
Companest receives inbound task requests from the master, processes them
through the orchestrator, and sends results back.

Message flow:
    1. Master sends: {type:"req", id:"abc", method:"task.team", params:{...}}
    2. Companest processes through orchestrator
    3. Companest responds: {type:"res", id:"abc", ok:true, payload:{...}}

Supported inbound methods:
    - task.team: Route to a specific Pi Agent Team
    - task.auto: Auto-detect team from content and execute
    - task.execute: Deprecated  redirects to task.auto
    - ping: Health check
    - status: Teams + orchestrator status
"""

import asyncio
import logging
import re
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from .client import GatewayClient
from .config import MasterConfig
from .exceptions import MasterError, OrchestratorError

logger = logging.getLogger(__name__)

# Max result size sent back to user (chars)
_MAX_RESULT_SIZE = 50_000
_DEDUP_WINDOW = 10.0  # seconds  identical requests within this window are deduped


class MasterConnection:
    """
    Manages the WebSocket connection to a master gateway.

    Listens for inbound task requests from the master, dispatches them
    through the orchestrator, and sends results back. Concurrency is
    controlled via an asyncio.Semaphore.

    Example:
        conn = MasterConnection(config.master, orchestrator)
        await conn.start()   # blocks until stopped
        await conn.stop()
    """

    def __init__(self, config: MasterConfig, orchestrator):
        """
        Args:
            config: MasterConfig with connection settings
            orchestrator: CompanestOrchestrator for task execution
        """
        self.config = config
        self.orchestrator = orchestrator

        self._client: Optional[GatewayClient] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tasks)
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

        # Per-user rate limiting: user_id -> list of request timestamps
        self._user_requests: Dict[str, list] = {}
        self._rate_limit_rpm = getattr(config, "rate_limit_rpm", 30)

        # Request deduplication: (user_id, task_hash) -> timestamp
        self._recent_requests: Dict[str, float] = {}

    async def start(self) -> None:
        """Connect to master and listen for inbound requests."""
        if not self.config.host:
            raise MasterError("Master host not configured")

        self._running = True

        self._client = GatewayClient(
            ws_url=self.config.ws_url,
            auth_token=self.config.auth_token,
            auth_password=self.config.auth_password,
            role="controller",
            scopes=["agent", "sessions", "config"],
            reconnect=self.config.reconnect,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            on_inbound_request=self._on_inbound_request,
        )

        logger.info(f"Connecting to master at {self.config.ws_url}...")

        try:
            await self._client.connect()
            logger.info(f"Connected to master at {self.config.ws_url}")
        except Exception as e:
            logger.error(f"Failed to connect to master: {e}")
            raise MasterError(f"Failed to connect to master: {e}")

        # Keep running until stopped  the client's receive loop
        # handles reconnection internally
        try:
            while self._running and self._client.is_connected:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Disconnect and cancel active tasks."""
        self._running = False

        # Cancel all active task processors
        for task_id, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._active_tasks.clear()

        # Disconnect from master
        if self._client:
            await self._client.disconnect()
            self._client = None

        logger.info("Master connection stopped")

    def _on_inbound_request(self, frame: dict) -> None:
        """
        Callback from the gateway client when an inbound req frame arrives.

        Spawns an asyncio.Task to handle the request asynchronously.
        This callback runs synchronously in the receive loop context,
        so we must not block.
        """
        request_id = frame.get("id")
        method = frame.get("method", "")

        if not request_id:
            logger.warning("Inbound request missing id, ignoring")
            return

        logger.info(f"Inbound request: method={method} id={request_id}")

        task = asyncio.create_task(
            self._handle_inbound_request(request_id, method, frame.get("params", {}))
        )
        self._active_tasks[request_id] = task

        def _task_done(t):
            self._active_tasks.pop(request_id, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"Unhandled exception in request {request_id}: {exc}")

        task.add_done_callback(_task_done)

    async def _handle_inbound_request(
        self, request_id: str, method: str, params: dict
    ) -> None:
        """Dispatch an inbound request to the appropriate handler."""
        try:
            if method == "task.team":
                await self._handle_task_team(request_id, params)
            elif method in ("task.auto", "task.execute"):
                if method == "task.execute":
                    logger.warning(
                        f"[{request_id[:8]}] task.execute is deprecated, "
                        "redirecting to task.auto"
                    )
                await self._handle_task_auto(request_id, params)
            elif method == "ping":
                await self._send_response(request_id, True, {"pong": True})
            elif method == "status":
                await self._handle_status(request_id)
            else:
                await self._send_response(
                    request_id, False,
                    error={"message": f"Unknown method: {method}", "code": "unknown_method"},
                )
        except asyncio.CancelledError:
            raise
        except MasterError:
            # Response delivery failure  already logged, nothing more to do
            pass
        except Exception as e:
            logger.error(f"Error handling request {request_id}: {e}")
            try:
                await self._send_response(
                    request_id, False,
                    error={"message": str(e), "code": "internal_error"},
                )
            except MasterError:
                logger.error(f"Could not send error response for {request_id}: user will not be notified")

    def _validate_task_input(self, task: str, max_length: int = 50_000) -> Optional[str]:
        """Validate task input. Returns error message or None."""
        if not task:
            return "Missing 'task' parameter"
        if not isinstance(task, str):
            return "'task' must be a string"
        if len(task) > max_length:
            return f"Task too long ({len(task)} chars, max {max_length})"
        self._check_injection(task)
        return None
    # Security filters

    _INJECTION_PATTERNS = re.compile(
        r"(?i)("
        r"ignore\s+(all\s+)?previous\s+instructions?"
        r"|system\s+prompt"
        r"|reveal\s+(your\s+)?(api\s*)?key"
        r"|env(ironment)?\s+variable"
        r"|ANTHROPIC_API"
        r"|OPENAI_API"
        r"|LITELLM_MASTER"
        r"|print\s+.*\bos\.environ"
        r"|bypassPermissions"
        r")"
    )

    # Matches literal secret values (key patterns) for redaction
    _SECRET_VALUE_PATTERNS = re.compile(
        r"(?:"
        r"sk-ant-api[0-9A-Za-z\-_]{10,}"     # Anthropic keys
        r"|sk-proj-[0-9A-Za-z]{10,}"           # OpenAI project keys
        r"|sk-[a-zA-Z0-9]{20,}"                # Generic OpenAI keys
        r"|AKIA[0-9A-Z]{16}"                   # AWS access key IDs
        r")"
    )

    # Matches env-style key assignments: DEEPSEEK_API_KEY=xxx or COMPANEST_API_TOKEN: "xxx"
    _SECRET_ASSIGN_PATTERN = re.compile(
        r"((?:DEEPSEEK_API_KEY|MOONSHOT_API_KEY|ZHIPU_API_KEY|DASHSCOPE_API_KEY"
        r"|LITELLM_MASTER_KEY|COMPANEST_API_TOKEN|COMPANEST_MASTER_TOKEN"
        r"|BRAVE_API_KEY|X_BEARER_TOKEN)"
        r"\s*[=:]\s*['\"]?)"                    # prefix kept visible
        r"([^\s'\"]{8,})"                       # value to redact
    )

    def _check_injection(self, task: str) -> None:
        """Log warning if task contains suspicious injection patterns.

        Does not reject or strip  too many false positives.
        Output sanitization is handled separately by _sanitize_output().
        """
        matches = self._INJECTION_PATTERNS.findall(task)
        if matches:
            logger.warning(
                f"[Security] Potential injection detected: "
                f"{[m[0] if isinstance(m, tuple) else m for m in matches][:3]}"
            )

    @classmethod
    def _sanitize_output(cls, result: str) -> str:
        """Redact API keys and secrets from output before sending to user."""
        result = cls._SECRET_VALUE_PATTERNS.sub("[REDACTED]", result)
        result = cls._SECRET_ASSIGN_PATTERN.sub(r"\1[REDACTED]", result)
        return result

    def _extract_user_context(self, params: dict) -> dict:
        """Extract user context from request params.

        Includes optional binding hints from the gateway (legacy transition):
        binding_team_id / binding_mode are set when the gateway had a local
        static binding match.  Companest may honour or override these.
        """
        ctx: dict = {
            "user_id": params.get("user_id", ""),
            "chat_id": params.get("chat_id", ""),
            "channel": params.get("channel", "telegram"),
        }
        # Pass through binding hints if present
        if params.get("binding_team_id"):
            ctx["binding_team_id"] = params["binding_team_id"]
        if params.get("binding_mode"):
            ctx["binding_mode"] = params["binding_mode"]
        return ctx

    async def send_notification(
        self, chat_id: str, channel: str, user_id: str, message: str,
    ) -> None:
        """Send a notification to the user via the gateway."""
        if not self._client or not self._client.is_connected:
            logger.warning("Cannot send notification: not connected to master")
            return
        try:
            await self._client.send_notification(chat_id, channel, user_id, message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _check_rate_limit(self, user_id: str) -> Optional[str]:
        """Check per-user rate limit. Returns error message or None."""
        if not user_id or self._rate_limit_rpm <= 0:
            return None
        now = time.monotonic()
        window = 60.0  # 1 minute
        timestamps = self._user_requests.get(user_id, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < window]
        self._user_requests[user_id] = timestamps
        if len(timestamps) >= self._rate_limit_rpm:
            return f"Rate limit exceeded ({self._rate_limit_rpm} requests/minute)"
        timestamps.append(now)
        return None

    def _check_dedup(self, user_id: str, task: str) -> Optional[str]:
        """Check for duplicate requests. Returns error message or None."""
        if not user_id or not task:
            return None
        dedup_key = f"{user_id}:{hash(task)}"
        now = time.monotonic()
        # Prune old entries periodically
        if len(self._recent_requests) > 1000:
            self._recent_requests = {
                k: v for k, v in self._recent_requests.items()
                if now - v < _DEDUP_WINDOW
            }
        last_time = self._recent_requests.get(dedup_key)
        if last_time is not None and now - last_time < _DEDUP_WINDOW:
            return "Duplicate request (still processing)"
        self._recent_requests[dedup_key] = now
        return None

    @staticmethod
    def _truncate_result(result: str) -> str:
        """Truncate result to max size to avoid oversized responses."""
        if len(result) <= _MAX_RESULT_SIZE:
            return result
        return result[:_MAX_RESULT_SIZE] + f"\n\n... (truncated, {len(result)} chars total)"

    async def _handle_task_team(self, request_id: str, params: dict) -> None:
        """Handle task.team  route to specific Pi Agent Team.

        Optional params:
            mode: "default" | "cascade" | "loop" | "council"
            cascade: bool (deprecated  use mode="cascade" instead)
        """
        task = params.get("task")
        team_id = params.get("team_id")
        err = self._validate_task_input(task)
        if err or not team_id:
            await self._send_response(
                request_id, False,
                error={"message": err or "Missing 'team_id' parameter", "code": "invalid_params"},
            )
            return

        # Rate limit and dedup checks
        user_id = params.get("user_id", "")
        rate_err = self._check_rate_limit(user_id)
        if rate_err:
            await self._send_response(
                request_id, False,
                error={"message": rate_err, "code": "rate_limited"},
            )
            return
        dedup_err = self._check_dedup(user_id, task)
        if dedup_err:
            await self._send_response(
                request_id, False,
                error={"message": dedup_err, "code": "duplicate"},
            )
            return

        if not hasattr(self.orchestrator, "run_team"):
            await self._send_response(
                request_id, False,
                error={"message": "Teams not initialized on orchestrator", "code": "not_available"},
            )
            return

        # Backward compat: cascade=true  mode="cascade" when mode not set
        mode = params.get("mode")
        if mode is None:
            mode = "cascade" if params.get("cascade") else "default"

        # Task priority for cost gate
        priority = params.get("priority", "normal")
        if priority not in ("critical", "high", "normal", "low"):
            priority = "normal"

        user_context = self._extract_user_context(params)
        on_progress = self._make_progress_callback(request_id)

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self.orchestrator.run_team(
                        task, team_id, mode=mode,
                        on_progress=on_progress,
                        user_context=user_context,
                        priority=priority,
                    ),
                    timeout=self.config.task_timeout,
                )
                await self._send_response(request_id, True, {
                    "result": self._truncate_result(self._sanitize_output(result)),
                    "team_id": team_id,
                    "mode": mode,
                })
            except asyncio.TimeoutError:
                await self._send_response(
                    request_id, False,
                    error={"message": f"Team task timed out after {self.config.task_timeout}s", "code": "timeout"},
                )
            except Exception as e:
                await self._send_response(
                    request_id, False,
                    error={"message": self._sanitize_output(str(e)), "code": "execution_error"},
                )

    async def _handle_task_auto(self, request_id: str, params: dict) -> None:
        """Handle task.auto  LLM-powered auto-routing via SmartRouter.

        Uses orchestrator.run_auto() which:
        1. Calls SmartRouter (Haiku LLM  keyword fallback)
        2. Supports multi-team execution (sequential/parallel)
        3. Explicitly declines when no team fits

        Optional params:
            mode: "default" | "cascade" | "loop" | "council" | None (router decides)
            cascade: bool (deprecated  use mode="cascade" instead)
        """
        task = params.get("task")
        err = self._validate_task_input(task)
        if err:
            await self._send_response(
                request_id, False,
                error={"message": err, "code": "invalid_params"},
            )
            return

        # Rate limit and dedup checks
        user_id = params.get("user_id", "")
        rate_err = self._check_rate_limit(user_id)
        if rate_err:
            await self._send_response(
                request_id, False,
                error={"message": rate_err, "code": "rate_limited"},
            )
            return
        dedup_err = self._check_dedup(user_id, task)
        if dedup_err:
            await self._send_response(
                request_id, False,
                error={"message": dedup_err, "code": "duplicate"},
            )
            return

        if not hasattr(self.orchestrator, "run_auto"):
            await self._send_response(
                request_id, False,
                error={"message": "Teams not initialized", "code": "not_available"},
            )
            return

        # Backward compat: cascade=true  mode="cascade" when mode not set
        mode = params.get("mode")
        if mode is None and params.get("cascade"):
            mode = "cascade"
        # mode=None means router decides (passed through to run_auto)

        user_context = self._extract_user_context(params)
        on_progress = self._make_progress_callback(request_id)

        async with self._semaphore:
            try:
                result, decision = await asyncio.wait_for(
                    self.orchestrator.run_auto(
                        task, mode=mode,
                        on_progress=on_progress,
                        user_context=user_context,
                    ),
                    timeout=self.config.task_timeout,
                )

                team_ids = [t.team_id for t in decision.teams]
                effective_mode = mode if mode is not None else decision.mode
                routing_note = (
                    f"[{'+'.join(team_ids)}]"
                    if decision.confidence >= 0.5
                    else f"[{'+'.join(team_ids)}?]"
                )

                await self._send_response(request_id, True, {
                    "result": self._truncate_result(self._sanitize_output(result)),
                    "team_ids": team_ids,
                    "strategy": decision.strategy,
                    "confidence": decision.confidence,
                    "reasoning": decision.reasoning,
                    "routing_note": routing_note,
                    "mode": effective_mode,
                })
            except OrchestratorError as e:
                if e.details.get("declined"):
                    # Graceful decline  not an error, just no suitable team
                    await self._send_response(request_id, True, {
                        "result": f"I'm not sure which team should handle this. {e.message}",
                        "team_ids": [],
                        "declined": True,
                        "reasoning": e.details.get("reasoning", ""),
                    })
                else:
                    await self._send_response(
                        request_id, False,
                        error={"message": self._sanitize_output(str(e)), "code": "execution_error"},
                    )
            except asyncio.TimeoutError:
                await self._send_response(
                    request_id, False,
                    error={"message": "Auto task timed out", "code": "timeout"},
                )
            except Exception as e:
                await self._send_response(
                    request_id, False,
                    error={"message": self._sanitize_output(str(e)), "code": "execution_error"},
                )

    async def _handle_status(self, request_id: str) -> None:
        """Handle status  return orchestrator and teams info."""
        status = {
            "orchestrator": self.orchestrator.get_status() if self.orchestrator else {},
            "master": {
                "connected": self._client.is_connected if self._client else False,
                "active_tasks": len(self._active_tasks),
                "max_concurrent_tasks": self.config.max_concurrent_tasks,
            },
        }
        if hasattr(self.orchestrator, "get_teams_status"):
            status["teams"] = self.orchestrator.get_teams_status()
        await self._send_response(request_id, True, status)

    async def _send_response(
        self,
        request_id: str,
        ok: bool,
        payload: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a response frame back to the master.

        Raises MasterError if the response cannot be delivered, so callers
        know the user won't receive the result.
        """
        if not self._client or not self._client.is_connected:
            logger.error(
                f"Cannot send response {request_id}: not connected. "
                "User will not receive this result."
            )
            raise MasterError(f"Response delivery failed for {request_id}: not connected")

        try:
            await self._client.send_response(request_id, ok, payload, error)
        except Exception as e:
            logger.error(f"Failed to send response {request_id}: {e}")
            raise MasterError(f"Response delivery failed for {request_id}: {e}")

    async def _send_progress(self, request_id: str, message: str) -> None:
        """Send a progress frame (fire-and-forget, never throws)."""
        try:
            if self._client and self._client.is_connected:
                await self._client.send_progress(request_id, message)
        except Exception:
            pass

    def _make_progress_callback(
        self, request_id: str,
    ) -> Callable[[str], Coroutine[Any, Any, None]]:
        """Create an on_progress callback bound to a request_id."""
        async def on_progress(message: str) -> None:
            await self._send_progress(request_id, message)
        return on_progress
