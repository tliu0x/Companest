from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from .company import CompanyConfig, CompanySchedule
    from ..events import EventBus
    from ..memory import MemoryManager, EnrichmentSource
    from ..agents.team import TeamConfig
    from ..agents.tools import ToolRegistry


@runtime_checkable
class CompanyComponent(Protocol):
    @property
    def company_id(self) -> str:
        ...

    @property
    def config(self) -> "CompanyConfig":
        ...

    def teams(self) -> "List[TeamConfig]":
        ...

    def schedules(self) -> "List[CompanySchedule]":
        ...

    def on_init(self, ctx: "CompanyContext") -> None:
        """Called once during orchestrator init."""

    def on_teardown(self) -> None:
        """Called on shutdown or company removal."""


@dataclass
class CompanyContext:
    """Registration context given to CompanyComponent.on_init()."""

    company_id: str
    memory: "CompanyMemoryNamespace"
    tool_registry: "ToolRegistry"
    event_bus: "EventBus"
    add_binding: Callable[[str, str, str], None]  # (pattern, team_id, mode)
    register_enrichment: Callable[["EnrichmentSource"], None]


class CompanyMemoryNamespace:
    """Company-scoped view of MemoryManager."""

    def __init__(self, manager: "MemoryManager", company_id: str) -> None:
        self._mgr = manager
        self._company_id = company_id
        self._ceo_team = f"company-{company_id}"

    @property
    def company_id(self) -> str:
        return self._company_id

    def _scoped_team_id(self, team_id: str) -> str:
        """Prefix *team_id* with the company namespace to enforce isolation."""
        if team_id.startswith(f"{self._company_id}/"):
            return team_id
        if team_id == self._ceo_team:
            return team_id
        if "/" in team_id:
            raise ValueError(
                f"Component in company '{self._company_id}' cannot access "
                f"team namespace '{team_id}' belonging to another company"
            )
        return f"{self._company_id}/{team_id}"

    def _shared_dir(self) -> Path:
        d = self._mgr.base_path / "companies" / self._company_id / "shared"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def read_shared(self, key: str) -> Any:
        self._mgr._validate_path_component(key, "key")
        return self._mgr._read_json(self._shared_dir() / key)

    def write_shared(self, key: str, data: Any) -> None:
        self._mgr._validate_path_component(key, "key")
        path = self._shared_dir() / key
        self._mgr._write_json(path, data)
        self._mgr._file_cache.pop(path.resolve(), None)

    def list_shared(self) -> List[str]:
        d = self._shared_dir()
        return sorted(
            f.name for f in d.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    def read_team_memory(self, team_id: str, key: str) -> Any:
        scoped = self._scoped_team_id(team_id)
        return self._mgr.read_team_memory(scoped, key)

    def write_team_memory(self, team_id: str, key: str, data: Any) -> None:
        scoped = self._scoped_team_id(team_id)
        self._mgr.write_team_memory(scoped, key, data)

    def list_team_memory(self, team_id: str) -> List[str]:
        scoped = self._scoped_team_id(team_id)
        return self._mgr.list_team_memory(scoped)

    def build_system_prompt(
        self, team_id: str, pi_id: str,
        company_context: Optional[str] = None,
    ) -> str:
        scoped = self._scoped_team_id(team_id)
        return self._mgr.build_system_prompt(scoped, pi_id, company_context)
