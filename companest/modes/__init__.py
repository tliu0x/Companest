from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

from ..exceptions import TeamError

if TYPE_CHECKING:
    from ..agents.pi import Pi
    from ..agents.team import AgentTeam

ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

VALID_MODES = ("default", "cascade", "loop", "council", "collaborative", "conditional")


def _with_memory_task_hint(
    user_context: Optional[Dict[str, Any]],
    task_hint: str,
) -> Dict[str, Any]:
    """Attach the original task so internal mode prompts retrieve the right memory."""
    ctx = dict(user_context) if user_context else {}
    if task_hint:
        ctx["memory_task_hint"] = task_hint
    return ctx


def _lead_pi(team: "AgentTeam") -> "Pi":
    if not team.lead_pi_id or team.lead_pi_id not in team.pis:
        raise TeamError(
            f"Team '{team.id}' has no valid lead_pi",
            details={"lead_pi": team.lead_pi_id, "available": list(team.pis.keys())},
        )
    return team.pis[team.lead_pi_id]


class ExecutionMode(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        ...


class ModeRegistry:
    def __init__(self):
        self._modes: Dict[str, ExecutionMode] = {}

    def register(self, mode: ExecutionMode, name: Optional[str] = None) -> None:
        key = name or mode.name
        self._modes[key] = mode

    def get(self, name: str) -> ExecutionMode:
        if name not in self._modes:
            raise KeyError(
                f"Unknown execution mode '{name}'. "
                f"Available: {', '.join(sorted(self._modes))}"
            )
        return self._modes[name]

    def list_modes(self) -> List[str]:
        return sorted(self._modes.keys())


def build_default_registry(run_team_fn=None) -> ModeRegistry:
    registry = ModeRegistry()
    registry.register(DefaultMode(cascade=False))
    registry.register(DefaultMode(cascade=True), name="cascade")
    registry.register(LoopMode())
    registry.register(CouncilMode())
    registry.register(CollaborativeMode())

    if run_team_fn is not None:
        registry.register(ConditionalMode(run_team_fn=run_team_fn))

    return registry


# Imported last: the mode modules import the names defined above.
from .default import DefaultMode
from .loop import LoopMode
from .council import CouncilMode
from .collaborative import CollaborativeMode
from .conditional import ConditionalMode

__all__ = [
    "ExecutionMode",
    "ModeRegistry",
    "build_default_registry",
    "VALID_MODES",
    "ProgressCallback",
    "DefaultMode",
    "LoopMode",
    "CouncilMode",
    "CollaborativeMode",
    "ConditionalMode",
]
