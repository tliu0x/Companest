"""
Channel Adapter Layer

Abstracts messaging platforms behind a standard interface so the core
message-handling logic (progress updates, response formatting, error
handling) is written once and reused across Telegram, Discord, Slack, etc.

Adding a new platform = implement ChannelAdapter (~100 lines) + wire it
into master_gateway.py.  Zero changes to Companest core.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("channel")


#  Abstract Base 

class ChannelAdapter(ABC):
    """
    Standard interface every messaging platform must implement.

    The gateway calls these four methods; each adapter translates
    them into platform-specific API calls.
    """

    @abstractmethod
    async def send_thinking(self, chat_id: str, text: str) -> Any:
        """Send an initial "thinking" message. Returns a message reference
        that can be passed to edit_message() later."""
        ...

    @abstractmethod
    async def edit_message(self, ref: Any, text: str) -> None:
        """Edit a previously sent message (for progress + final result).
        Must silently swallow errors (message may have been deleted, etc.)."""
        ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a new standalone message."""
        ...

    @abstractmethod
    def max_message_length(self) -> int:
        """Platform's maximum message length in characters."""
        ...

    @property
    def channel_name(self) -> str:
        """Platform identifier (e.g. 'telegram', 'discord')."""
        return "unknown"


#  Telegram Adapter 

class TelegramAdapter(ChannelAdapter):
    """Adapter for python-telegram-bot v20+."""

    def __init__(self, bot):
        self._bot = bot

    @property
    def channel_name(self) -> str:
        return "telegram"

    async def send_thinking(self, chat_id: str, text: str) -> Any:
        return await self._bot.send_message(chat_id=int(chat_id), text=text)

    async def edit_message(self, ref: Any, text: str) -> None:
        text = self._truncate(text)
        try:
            await ref.edit_text(text)
        except Exception:
            pass  # message deleted / rate-limited / unchanged

    async def send_message(self, chat_id: str, text: str) -> None:
        text = self._truncate(text)
        await self._bot.send_message(chat_id=int(chat_id), text=text)

    def max_message_length(self) -> int:
        return 4096

    def _truncate(self, text: str) -> str:
        limit = self.max_message_length() - 96  # safety margin
        if len(text) > limit:
            return text[:limit] + "\n\n... (truncated)"
        return text


#  Binding (static routing rules) 

@dataclass
class Binding:
    """A static routing rule that bypasses SmartRouter.

    Any field set to None is treated as a wildcard (matches anything).
    Rules are matched in priority order (highest first).
    """
    team_id: str
    mode: str = "default"
    channel: Optional[str] = None   # "telegram" / "discord" / None
    chat_id: Optional[str] = None   # specific group or DM
    user_id: Optional[str] = None   # specific user
    priority: int = 0


def match_binding(
    bindings: list[Binding],
    channel: str,
    chat_id: str,
    user_id: str,
) -> Optional[Binding]:
    """Find the highest-priority binding that matches the given context.

    Returns None if no binding matches (caller should fall back to SmartRouter).
    """
    candidates = []
    for b in bindings:
        if b.channel is not None and b.channel != channel:
            continue
        if b.chat_id is not None and b.chat_id != chat_id:
            continue
        if b.user_id is not None and b.user_id != user_id:
            continue
        candidates.append(b)

    if not candidates:
        return None

    return max(candidates, key=lambda b: b.priority)


def load_bindings(path: str) -> list[Binding]:
    """Load bindings from a JSON file.

    Expected format:
        [
          {"team_id": "philosophy", "mode": "council",
           "channel": "telegram", "chat_id": "-100123456", "priority": 10},
          ...
        ]
    """
    import json
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        valid_fields = {f.name for f in __import__("dataclasses").fields(Binding)}
        bindings = [
            Binding(**{k: v for k, v in entry.items() if k in valid_fields})
            for entry in raw
        ]
        logger.info(f"Loaded {len(bindings)} binding(s) from {path}")
        return bindings
    except FileNotFoundError:
        logger.warning(f"Bindings file not found: {path} (no static routing)")
        return []
    except Exception as e:
        logger.error(f"Failed to load bindings from {path}: {e}")
        return []


#  Response Formatting 

def format_response(response: dict, elapsed: int) -> str:
    """Format a Companest response into a user-facing message with footer.

    Works for both task.auto and task.team responses.
    """
    if not response.get("ok"):
        msg = response.get("error", {}).get("message", "Unknown error")
        return f"Error: {msg}"

    payload = response.get("payload", {})
    result = payload.get("result", "No result")

    # Build routing info footer
    team_ids = payload.get("team_ids", [])
    team_id = payload.get("team_id")
    teams_str = "+".join(team_ids) if team_ids else (team_id or "-")

    footer_parts = [f"\U0001f4cb {teams_str}"]

    mode = payload.get("mode", "")
    if mode:
        footer_parts.append(mode)

    confidence = payload.get("confidence")
    if confidence is not None:
        footer_parts.append(f"{confidence:.0%}")

    routing = payload.get("routing")
    if routing:
        footer_parts.append(routing)

    footer_parts.append(f"{elapsed}s")
    footer = " \u00b7 ".join(footer_parts)

    return f"{result}\n\n\u2014\n{footer}"


#  Generic Message Handler 

async def handle_incoming(
    gateway,
    adapter: ChannelAdapter,
    text: str,
    chat_id: str,
    user_id: str,
    binding: Optional[Binding] = None,
) -> None:
    """Platform-agnostic core message handler.

    All routing decisions are made on the Companest side (CompanyRegistry 
    GlobalBinding  SmartRouter).  The gateway always sends task.auto
    with the full user context; Companest resolves company, applies bindings,
    and routes accordingly.

    If a local *binding* is supplied it is included as a hint in the
    request (``binding_team_id`` / ``binding_mode``) so Companest can honour
    legacy static rules during the transition period.

    Args:
        gateway: MasterGateway instance (for send_request)
        adapter: Platform-specific ChannelAdapter
        text: User's message text
        chat_id: Chat/channel ID
        user_id: User ID
        binding: Optional local Binding hint (legacy, Companest does its own resolve)
    """
    if not gateway.is_companest_connected:
        await adapter.send_message(chat_id, "Companest is not connected. Please wait...")
        return

    ref = await adapter.send_thinking(chat_id, "\U0001f914 Routing...")
    start_time = asyncio.get_running_loop().time()

    # Rate-limited progress callback (min 1s between edits)
    _last_edit = 0.0

    async def on_progress(message: str):
        nonlocal _last_edit
        now = asyncio.get_running_loop().time()
        if now - _last_edit < 1.0:
            return
        _last_edit = now
        await adapter.edit_message(ref, message)

    try:
        # Full user context  Companest resolves company + routing
        params: dict = {
            "task": text,
            "session_id": chat_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "channel": adapter.channel_name,
        }

        # Pass local binding as hint (Companest may override with its own resolve)
        if binding:
            params["binding_team_id"] = binding.team_id
            params["binding_mode"] = binding.mode

        response = await gateway.send_request(
            "task.auto",
            params,
            timeout=300,
            on_progress=on_progress,
        )

        elapsed = int(asyncio.get_running_loop().time() - start_time)
        full_text = format_response(response, elapsed)

        # Truncate for platform limit
        limit = adapter.max_message_length() - 96
        if len(full_text) > limit:
            result = response.get("payload", {}).get("result", "")
            cut = limit - 200  # leave room for footer
            full_text = format_response(
                {"ok": True, "payload": {**response.get("payload", {}),
                                          "result": result[:cut] + "\n\n... (truncated)"}},
                elapsed,
            )

        await adapter.edit_message(ref, full_text)

    except asyncio.TimeoutError:
        await adapter.edit_message(ref, "Request timed out")
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await adapter.edit_message(ref, f"Error: {e}")
