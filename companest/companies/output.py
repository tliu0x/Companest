from abc import ABC, abstractmethod
from ..utils import run_sync


class OutputSink(ABC):
    """Output channel for CEO cycle results."""

    @abstractmethod
    async def emit(self, company_id: str, cycle_result: dict) -> None:
        ...


class MemorySink(OutputSink):
    def __init__(self, memory):
        self._memory = memory

    async def emit(self, company_id: str, cycle_result: dict) -> None:
        team_id = f"company-{company_id}"
        await run_sync(self._memory.append_team_memory, team_id, "cycle-results.json", cycle_result,
                       category="task", pinned=True)
