#!/usr/bin/env python3
"""
Master Gateway for Companest

Deploys on the master machine (e.g. AWS EC2). Provides:
1. Telegram bot  users send messages here
2. WebSocket server  Companest connects as a controller to receive tasks

Supports:
- Channel Adapter pattern: swap messaging platforms without touching core logic
- Binding routing: static rules that bypass SmartRouter for known contexts

Flow:
    User -> Channel Adapter -> Gateway WS -> Companest -> Gateway WS -> Channel Adapter -> User

Requirements (install on the gateway machine):
    pip install websockets python-telegram-bot

Usage:
    export TELEGRAM_BOT_TOKEN="your-token"
    python master_gateway.py --port 19000

    # With static routing bindings:
    python master_gateway.py --port 19000 --bindings bindings.json

    # Then on the Companest machine:
    python -m companest serve -c .companest/config.test.md
"""

import os
import sys
import json
import uuid
import asyncio
import logging
import argparse
from typing import Any, Callable, Coroutine, Dict, Optional

from channel import (
    TelegramAdapter,
    Binding,
    match_binding,
    load_bindings,
    handle_incoming,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("master-gateway")


# =============================================================================
# Gateway WebSocket Server
# =============================================================================

class MasterGateway:
    """
    WebSocket server that Companest connects to as a controller.

    When a user message arrives (via any ChannelAdapter), the gateway sends
    an inbound request to Companest via the WebSocket, waits for the response,
    and returns it. Supports progress frames for real-time status updates.
    """

    def __init__(self, port: int = 19000, auth_token: Optional[str] = None):
        self.port = port
        self.auth_token = auth_token
        self._controller_ws = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._progress_callbacks: Dict[
            str, Callable[[str], Coroutine[Any, Any, None]]
        ] = {}
        self._server = None
        self._notification_handler: Optional[
            Callable[[str, str, str], Coroutine[Any, Any, None]]
        ] = None  # async fn(chat_id, channel, message)

    @property
    def is_companest_connected(self) -> bool:
        return self._controller_ws is not None

    async def start(self):
        import websockets

        self._server = await websockets.serve(
            self._handle_connection, "0.0.0.0", self.port
        )
        logger.info(f"Gateway WS listening on ws://0.0.0.0:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, ws, path=None):
        """Handle an incoming WebSocket connection (Companest controller)."""
        logger.info("Incoming controller connection...")

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            frame = json.loads(raw)
        except Exception as e:
            logger.error(f"Handshake failed: {e}")
            return

        if frame.get("type") != "req" or frame.get("method") != "connect":
            logger.warning(f"Expected connect request, got: {frame.get('method')}")
            return

        # Verify auth token
        if self.auth_token:
            provided = frame.get("params", {}).get("auth")
            if provided != self.auth_token:
                await ws.send(json.dumps({
                    "type": "res",
                    "id": frame["id"],
                    "ok": False,
                    "error": {"message": "Invalid auth token"},
                }))
                return

        # Accept connection
        await ws.send(json.dumps({
            "type": "res",
            "id": frame["id"],
            "ok": True,
            "payload": {"message": "Connected as controller"},
        }))

        self._controller_ws = ws
        role = frame.get("params", {}).get("role", "unknown")
        logger.info(f"Companest connected (role={role})")

        # Listen for response and progress frames from Companest
        try:
            async for raw_msg in ws:
                try:
                    resp = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                frame_type = resp.get("type")

                if frame_type == "res":
                    req_id = resp.get("id")
                    self._progress_callbacks.pop(req_id, None)
                    future = self._pending.pop(req_id, None)
                    if future and not future.done():
                        future.set_result(resp)

                elif frame_type == "progress":
                    req_id = resp.get("id")
                    cb = self._progress_callbacks.get(req_id)
                    if cb:
                        msg = resp.get("payload", {}).get("message", "")
                        if msg:
                            asyncio.create_task(cb(msg))

                elif frame_type == "notification":
                    asyncio.create_task(self._handle_notification(resp))

        except Exception as e:
            logger.warning(f"Controller disconnected: {e}")
        finally:
            self._controller_ws = None
            # Fail all pending requests
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("Companest disconnected"))
            self._pending.clear()
            self._progress_callbacks.clear()
            logger.info("Companest disconnected")

    async def send_request(
        self,
        method: str,
        params: Optional[Dict] = None,
        timeout: float = 300,
        on_progress: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
    ) -> dict:
        """Send an inbound request to Companest and await the response.

        Args:
            method: RPC method name
            params: Optional parameters
            timeout: Request timeout in seconds
            on_progress: Optional async callback invoked with progress messages
        """
        if not self._controller_ws:
            raise RuntimeError("Companest is not connected")

        request_id = str(uuid.uuid4())
        frame: Dict[str, Any] = {
            "type": "req",
            "id": request_id,
            "method": method,
        }
        if params:
            frame["params"] = params

        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if on_progress:
            self._progress_callbacks[request_id] = on_progress

        await self._controller_ws.send(json.dumps(frame))
        logger.info(f"Sent {method} to Companest (id={request_id[:8]}...)")

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            self._progress_callbacks.pop(request_id, None)
            raise

    async def _handle_notification(self, frame: dict) -> None:
        """Handle a notification frame from Companest (push delivery)."""
        payload = frame.get("payload", {})
        chat_id = payload.get("chat_id", "")
        channel = payload.get("channel", "telegram")
        message = payload.get("message", "")

        if not chat_id or not message:
            logger.warning("Notification frame missing chat_id or message")
            return

        if self._notification_handler:
            try:
                await self._notification_handler(chat_id, channel, message)
            except Exception as e:
                logger.error(f"Notification handler error: {e}")
        else:
            logger.warning(f"No notification handler registered, dropping notification for {chat_id}")


# =============================================================================
# Telegram Bot
# =============================================================================

async def run_telegram_bot(
    gateway: MasterGateway,
    token: str,
    bindings: list[Binding],
):
    """Start the Telegram bot and bridge messages to Companest via gateway."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    app = Application.builder().token(token).build()
    adapter = TelegramAdapter(app.bot)

    #  Commands (Telegram-specific, simple enough to stay here) 

    async def cmd_start(update: Update, context):
        status = "connected" if gateway.is_companest_connected else "NOT connected"
        binding_count = len(bindings)
        await update.message.reply_text(
            f"Companest Gateway Bot\n\n"
            f"Companest status: {status}\n"
            f"Bindings: {binding_count} rule(s)\n\n"
            f"Send any message to route through Companest.\n"
            f"Commands: /ping /status"
        )

    async def cmd_ping(update: Update, context):
        if not gateway.is_companest_connected:
            await update.message.reply_text("Companest is not connected")
            return
        try:
            resp = await gateway.send_request("ping", timeout=5)
            if resp.get("ok"):
                await update.message.reply_text("Pong!")
            else:
                await update.message.reply_text("Ping failed")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_status(update: Update, context):
        if not gateway.is_companest_connected:
            await update.message.reply_text("Companest is not connected")
            return
        try:
            resp = await gateway.send_request("status", timeout=10)
            if resp.get("ok"):
                payload = resp.get("payload", {})
                lines = [
                    "Fleet Status:",
                    f"  Connected: {gateway.is_companest_connected}",
                ]
                teams = payload.get("teams", {})
                if teams:
                    registered = teams.get("registered", [])
                    active = teams.get("active", [])
                    lines.append(f"  Teams: {len(registered)} registered, {len(active)} active")
                master = payload.get("master", {})
                if master:
                    lines.append(f"  Active tasks: {master.get('active_tasks', 0)}")
                if bindings:
                    lines.append(f"  Bindings: {len(bindings)} rule(s)")
                await update.message.reply_text("\n".join(lines))
            else:
                msg = resp.get("error", {}).get("message", "Unknown error")
                await update.message.reply_text(f"Error: {msg}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    #  Core message handler (uses generic handle_incoming) 

    async def on_message(update: Update, context):
        text = update.message.text
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)

        binding = match_binding(bindings, "telegram", chat_id, user_id)

        await handle_incoming(
            gateway=gateway,
            adapter=adapter,
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            binding=binding,
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Register notification handler for push delivery (scheduled tasks, etc.)
    async def notification_handler(chat_id: str, channel: str, message: str):
        if channel == "telegram":
            await adapter.send_message(chat_id, message)
        else:
            logger.warning(f"Unsupported notification channel: {channel}")

    gateway._notification_handler = notification_handler

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Telegram bot started")

    return app


# =============================================================================
# Main
# =============================================================================

async def main(args):
    token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error(
            "No Telegram bot token provided.\n"
            "  Set TELEGRAM_BOT_TOKEN env var or pass --telegram-token"
        )
        sys.exit(1)

    auth_token = args.auth_token or os.getenv("COMPANEST_MASTER_TOKEN")

    # Load static routing bindings
    bindings_path = args.bindings or os.getenv("COMPANEST_BINDINGS")
    bindings = load_bindings(bindings_path) if bindings_path else []

    # Start gateway WS server
    gateway = MasterGateway(port=args.port, auth_token=auth_token)
    await gateway.start()

    # Start Telegram bot
    bot_app = await run_telegram_bot(gateway, token, bindings)

    logger.info("=" * 55)
    logger.info("  Master Gateway running!")
    logger.info(f"  WS server:  ws://0.0.0.0:{args.port}")
    logger.info(f"  Auth token: {'set' if auth_token else 'none (open)'}")
    logger.info(f"  Bindings:   {len(bindings)} rule(s)")
    logger.info(f"  Telegram:   active")
    logger.info(f"  Companest:       waiting for connection...")
    logger.info("")
    logger.info("  Start Companest in another terminal:")
    logger.info("    python -m companest serve -c .companest/config.test.md")
    logger.info("=" * 55)

    try:
        await asyncio.Future()  # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        await gateway.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Master Gateway for Companest")
    parser.add_argument(
        "--telegram-token",
        default=None,
        help="Telegram bot token (or set TELEGRAM_BOT_TOKEN env var)",
    )
    parser.add_argument(
        "--port", type=int, default=19000,
        help="WebSocket server port for Companest connection (default: 19000)",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Auth token for Companest controller connection (or set COMPANEST_MASTER_TOKEN)",
    )
    parser.add_argument(
        "--bindings",
        default=None,
        help="Path to bindings.json for static routing rules (or set COMPANEST_BINDINGS)",
    )

    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nBye!")
