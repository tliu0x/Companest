"""
Companest Company Component System

Defines the pluggable company interface so each company is a self-contained
component pack rather than just a config file.

CompanyComponent: Protocol that companies implement (teams, schedules, hooks).
CompanyContext:   Registration context passed to on_init().
CompanyMemoryNamespace: Company-scoped view of MemoryManager.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Optional, Protocol, TYPE_CHECKING, runtime_checkable,
)

if TYPE_CHECKING:
    from .company import CompanyConfig, CompanySchedule
    from .events import EventBus
    from .memory import MemoryManager, EnrichmentSource
    from .team import TeamConfig
    from .tools import ToolRegistry

logger = logging.getLogger(__name__)


#  Protocol 


@runtime_checkable
class CompanyComponent(Protocol):
    """
    A pluggable company definition.

    Implement this to define a company as code instead of (or in addition to)
    a static company.yaml.  The orchestrator will call the lifecycle methods
    during init and teardown.
    """

    @property
    def company_id(self) -> str:
        """Unique company identifier."""
        ...

    @property
    def config(self) -> "CompanyConfig":
        """Return the company configuration."""
        ...

    def teams(self) -> "List[TeamConfig]":
        """Return private team configs for this company."""
        ...

    def schedules(self) -> "List[CompanySchedule]":
        """Return scheduled tasks beyond what is in CompanyConfig."""
        ...

    def on_init(self, ctx: "CompanyContext") -> None:
        """Called once during orchestrator init.

        Use *ctx* to register enrichments, tools, routing bindings, etc.
        """
        ...

    def on_teardown(self) -> None:
        """Called on shutdown or company removal."""
        ...


#  Context passed to on_init 


@dataclass
class CompanyContext:
    """Registration context given to CompanyComponent.on_init()."""

    company_id: str
    memory: "CompanyMemoryNamespace"
    tool_registry: "ToolRegistry"
    event_bus: "EventBus"
    add_binding: Callable[[str, str, str], None]
    """add_binding(pattern, team_id, mode)  register a routing binding."""
    register_enrichment: Callable[["EnrichmentSource"], None]
    """register_enrichment(source)  inject enrichment into system prompts."""


#  Company Memory Namespace 


class CompanyMemoryNamespace:
    """
    Company-scoped view of MemoryManager.

    Provides:
    - **Shared memory** under ``companies/{company_id}/shared/`` for data
      that is cross-team within one company.
    - **Delegated team memory** that auto-prefixes team_id so each company
      cannot accidentally read another company's private teams.

    This is an *additive* wrapper  it does not replace MemoryManager.
    """

    def __init__(self, manager: "MemoryManager", company_id: str) -> None:
        self._mgr = manager
        self._company_id = company_id
        self._ceo_team = f"company-{company_id}"

    @property
    def company_id(self) -> str:
        return self._company_id

    #  Scoped team ID helper 

    def _scoped_team_id(self, team_id: str) -> str:
        """Prefix *team_id* with the company namespace to enforce isolation."""
        # Already namespaced to this company
        if team_id.startswith(f"{self._company_id}/"):
            return team_id
        # CEO team (convention: company-{id})
        if team_id == self._ceo_team:
            return team_id
        # Another company's namespace → reject
        if "/" in team_id:
            raise ValueError(
                f"Component in company '{self._company_id}' cannot access "
                f"team namespace '{team_id}' belonging to another company"
            )
        # Bare team name → auto-prefix with company namespace
        return f"{self._company_id}/{team_id}"

    #  Company-level shared memory 

    def _shared_dir(self) -> Path:
        d = self._mgr.base_path / "companies" / self._company_id / "shared"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def read_shared(self, key: str) -> Any:
        """Read from the company's shared memory namespace."""
        self._mgr._validate_path_component(key, "key")
        return self._mgr._read_json(self._shared_dir() / key)

    def write_shared(self, key: str, data: Any) -> None:
        """Write to the company's shared memory namespace."""
        self._mgr._validate_path_component(key, "key")
        path = self._shared_dir() / key
        self._mgr._write_json(path, data)
        self._mgr._file_cache.pop(path.resolve(), None)

    def list_shared(self) -> List[str]:
        """List keys in the company's shared memory."""
        d = self._shared_dir()
        if not d.exists():
            return []
        return sorted(
            f.name for f in d.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    #  Delegated team memory (auto-prefixed) 

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
