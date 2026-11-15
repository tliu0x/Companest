from typing import Any, Dict, Optional, TYPE_CHECKING

from . import ExecutionMode, ProgressCallback, _lead_pi

if TYPE_CHECKING:
    from ..agents.team import AgentTeam


class DefaultMode(ExecutionMode):
    """Execute task with lead Pi. Optionally uses model cascade."""

    def __init__(self, cascade: bool = False):
        self._cascade = cascade

    @property
    def name(self) -> str:
        return "cascade" if self._cascade else "default"

    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        pi = _lead_pi(team)
        if on_progress:
            await on_progress(
                f"\U0001f916 {team.id}/{pi.id} ({pi.model.split('/')[-1]})"
            )
        return await pi.run(
            task, cascade=self._cascade, on_progress=on_progress,
            user_context=user_context,
        )
