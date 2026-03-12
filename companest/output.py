"""
Companest Output Sinks

CEO cycle results are dispatched through OutputSink instances.
MemorySink is always registered (results never lost).
Additional sinks (webhook, callback, telegram) are optional.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)


class OutputSink(ABC):
    """Base class for CEO cycle result output channels."""

    @abstractmethod
    async def emit(self, company_id: str, cycle_result: dict) -> None:
        """Emit a CEO cycle result to this output channel."""


class MemorySink(OutputSink):
    """Default sink: persist cycle results to company memory (always enabled)."""

    def __init__(self, memory):
        self._memory = memory

    async def emit(self, company_id: str, cycle_result: dict) -> None:
        team_id = f"company-{company_id}"
        self._memory.append_team_memory(team_id, "cycle-results.json", cycle_result)


class WebhookSink(OutputSink):
    """Push cycle results to an HTTP webhook endpoint."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        self._url = url
        self._headers = headers or {}

    async def emit(self, company_id: str, cycle_result: dict) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    self._url,
                    json={"company_id": company_id, **cycle_result},
                    headers=self._headers,
                )
        except Exception as e:
            logger.error(f"[WebhookSink] Failed for {company_id}: {e}")


class CallbackSink(OutputSink):
    """Invoke a Python async callback (for SDK users)."""

    def __init__(self, callback: Callable[[str, dict], Coroutine[Any, Any, None]]):
        self._callback = callback

    async def emit(self, company_id: str, cycle_result: dict) -> None:
        await self._callback(company_id, cycle_result)


class TelegramReportSink(OutputSink):
    """Optional: push CEO cycle summaries to a Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def emit(self, company_id: str, cycle_result: dict) -> None:
        import httpx
        summary = cycle_result.get("summary", str(cycle_result)[:500])
        text = f"[{company_id}] CEO Cycle #{cycle_result.get('cycle', '-')}\n{summary}"
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text[:4096],
                    "parse_mode": "HTML",
                })
        except Exception as e:
            logger.error(f"[TelegramReportSink] Failed for {company_id}: {e}")
