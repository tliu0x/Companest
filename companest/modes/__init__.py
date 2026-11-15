"""Companest Execution Modes"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..team import AgentTeam

# Callback type: async fn(str) -> None
ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

# Valid mode names for router validation
VALID_MODES = ("default", "cascade", "loop", "council", "collaborative", "conditional")


class ExecutionMode(ABC):
    """Abstract base for execution modes."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique mode name (e.g. 'default', 'cascade', 'loop', 'council')."""
        ...

    @abstractmethod
    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute the task using this mode's strategy."""
        ...


class ModeRegistry:
    """Registry of available execution modes. Supports lookup by name."""

    def __init__(self):
        self._modes: Dict[str, ExecutionMode] = {}

    def register(self, mode: ExecutionMode, name: Optional[str] = None) -> None:
        """Register a mode under its name (or an explicit override)."""
        key = name or mode.name
        self._modes[key] = mode

    def get(self, name: str) -> ExecutionMode:
        """Get a mode by name. Raises KeyError if not found."""
        if name not in self._modes:
            raise KeyError(
                f"Unknown execution mode '{name}'. "
                f"Available: {', '.join(sorted(self._modes))}"
            )
        return self._modes[name]

    def list_modes(self) -> List[str]:
        """List all registered mode names."""
        return sorted(self._modes.keys())


def build_default_registry(run_team_fn=None) -> ModeRegistry:
    """Build a ModeRegistry with all built-in modes pre-registered."""
    from .default import DefaultMode
    from .loop import LoopMode
    from .council import CouncilMode
    from .collaborative import CollaborativeMode

    registry = ModeRegistry()
    registry.register(DefaultMode(cascade=False))
    registry.register(DefaultMode(cascade=True), name="cascade")
    registry.register(LoopMode())
    registry.register(CouncilMode())
    registry.register(CollaborativeMode())

    if run_team_fn is not None:
        from .conditional import ConditionalMode
        registry.register(ConditionalMode(run_team_fn=run_team_fn))

    return registry


# Re-export mode classes for convenience
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
