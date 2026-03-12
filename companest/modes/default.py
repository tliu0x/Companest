"""
Default execution mode  single Pi, optional cascade.

DefaultMode(cascade=False)  "default" mode
DefaultMode(cascade=True)   registered as "cascade" mode
"""

from typing import Any, Dict, Optional

from . import ExecutionMode, ProgressCallback
from ..exceptions import TeamError

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..team import AgentTeam


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
        if not team.lead_pi_id or team.lead_pi_id not in team.pis:
            raise TeamError(
                f"Team '{team.id}' has no valid lead_pi",
                details={"lead_pi": team.lead_pi_id, "available": list(team.pis.keys())},
            )
        pi = team.pis[team.lead_pi_id]
        if on_progress:
            await on_progress(
                f"\U0001f916 {team.id}/{pi.id} ({pi.model.split('/')[-1]})"
            )
        return await pi.run(
            task, cascade=self._cascade, on_progress=on_progress,
            user_context=user_context,
        )
