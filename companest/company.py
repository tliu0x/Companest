"""
Companest Company Registry

Multi-tenant company layer  each company is an autonomous business unit
with its own CEO Agent, bindings, budget, and schedules.

Company configs live in /data/companest/companies/{id}/company.yaml (sensitive, not in git).
Global bindings live in /data/companest/bindings.yaml.

Resolve priority:
1. CompanyBinding  route to company's CEO Agent
2. GlobalBinding  route directly to a specific team
3. SmartRouter auto-route (no match)
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .exceptions import CompanyError, CompanestError

logger = logging.getLogger(__name__)


#  Path validation 

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_company_id(value: str) -> str:
    """Validate company ID to prevent path traversal."""
    if not value:
        raise CompanyError("Empty company ID")
    if ".." in value or "/" in value or "\\" in value:
        raise CompanyError(f"Invalid company ID: path traversal not allowed: {value!r}")
    if not _SAFE_ID_RE.match(value):
        raise CompanyError(
            f"Invalid company ID: {value!r} "
            "(must start with alphanumeric, contain only alphanumeric/dash/underscore)"
        )
    return value


#  Data Models 


class CompanyBinding(BaseModel):
    """Identifies which incoming requests belong to this company."""
    channel: Optional[str] = None    # "telegram", "api", None=wildcard
    chat_id: Optional[str] = None
    user_id: Optional[str] = None

    def matches(self, channel: Optional[str], chat_id: Optional[str], user_id: Optional[str]) -> bool:
        """Check if an incoming request matches this binding."""
        if self.channel is not None and self.channel != channel:
            return False
        if self.chat_id is not None and self.chat_id != chat_id:
            return False
        if self.user_id is not None and self.user_id != user_id:
            return False
        # At least one field must be set to match
        return self.channel is not None or self.chat_id is not None or self.user_id is not None


class GlobalBinding(BaseModel):
    """Static routing rule  route a chat directly to a team (not via company)."""
    channel: Optional[str] = None
    chat_id: Optional[str] = None
    user_id: Optional[str] = None
    team_id: str
    mode: str = "cascade"
    priority: int = 0

    def matches(self, channel: Optional[str], chat_id: Optional[str], user_id: Optional[str]) -> bool:
        if self.channel is not None and self.channel != channel:
            return False
        if self.chat_id is not None and self.chat_id != chat_id:
            return False
        if self.user_id is not None and self.user_id != user_id:
            return False
        return self.chat_id is not None or self.user_id is not None or self.channel is not None


class CompanySchedule(BaseModel):
    """A scheduled task for the company's CEO Agent."""
    name: str
    team_id: str
    prompt: str
    interval_seconds: int = 1800
    mode: str = "cascade"
    enabled: bool = True


class CompanyCEOConfig(BaseModel):
    """Configuration for the company's CEO Agent (a special Pi)."""
    model: str = "claude-sonnet-4-5-20250929"
    max_turns: int = 50
    cycle_interval: int = 1800       # CEO operating cycle interval (seconds)
    cycle_prompt: Optional[str] = None   # Override: if set, use this instead of ceo_engine
                                         # If None, ceo_engine generates structured prompt
    goals: List[str] = Field(default_factory=list)        # Operating goals
    kpis: Dict[str, str] = Field(default_factory=dict)    # Key performance indicators
    enabled: bool = True


class CompanyPreferences(BaseModel):
    """Company-level preferences for task routing and budgets."""
    default_mode: str = "cascade"
    preferred_teams: List[str] = Field(default_factory=list)
    budget_hourly_usd: float = 1.0    # CEO hourly budget cap
    budget_monthly_usd: float = 200.0  # monthly total budget cap


class CompanyConfig(BaseModel):
    """Full configuration for a single company tenant."""
    id: str
    name: str
    domain: str = ""                  # domain knowledge  CEO system prompt
    bindings: List[CompanyBinding] = Field(default_factory=list)
    preferences: CompanyPreferences = Field(default_factory=CompanyPreferences)
    ceo: CompanyCEOConfig = Field(default_factory=CompanyCEOConfig)
    schedules: List[CompanySchedule] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)  # private env vars
    enabled: bool = True
    shared_teams: Optional[List[str]] = None
    routing_bindings: List[Dict[str, str]] = Field(default_factory=list)
    memory_seed: Dict[str, Any] = Field(default_factory=dict)
    mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _validate_company_id(v)


#  Resolve Result 


class ResolveResult:
    """Result of CompanyRegistry.resolve()  either company or global binding."""
    __slots__ = ("company", "global_binding")

    def __init__(
        self,
        company: Optional[CompanyConfig] = None,
        global_binding: Optional[GlobalBinding] = None,
    ):
        self.company = company
        self.global_binding = global_binding

    @property
    def matched(self) -> bool:
        return self.company is not None or self.global_binding is not None


#  Company Registry 


class CompanyRegistry:
    """
    Scans /data/companest/companies/ for company.yaml files and manages
    company lifecycle. Also loads global bindings from /data/companest/bindings.yaml.
    """

    def __init__(self, data_dir: str):
        self._data_dir = Path(data_dir)
        self._companies_dir = self._data_dir / "companies"
        self._bindings_path = self._data_dir / "bindings.yaml"
        self._configs: Dict[str, CompanyConfig] = {}
        self._global_bindings: List[GlobalBinding] = []
        self._mtimes: Dict[str, float] = {}  # company_id -> mtime
        self._bindings_mtime: float = 0.0
        self._components: Dict[str, Any] = {}  # company_id -> CompanyComponent

    #  Component registration 

    def register_component(self, component: Any) -> None:
        """Register a CompanyComponent.

        The component's config is stored alongside YAML-loaded configs.
        Components take precedence over YAML files with the same id.
        """
        cid = component.company_id
        self._configs[cid] = component.config
        self._components[cid] = component
        logger.info(f"[CompanyRegistry] Registered component: {cid}")

    def get_component(self, company_id: str) -> Optional[Any]:
        """Return the CompanyComponent for *company_id*, or None."""
        return self._components.get(company_id)

    def list_components(self) -> List[str]:
        """Return IDs of companies that have a registered component."""
        return list(self._components.keys())

    def scan(self) -> None:
        """Scan companies directory and load all company.yaml files."""
        self._configs.clear()
        self._mtimes.clear()

        # Re-inject component-backed companies (survive reload)
        for cid, comp in self._components.items():
            self._configs[cid] = comp.config

        if not self._companies_dir.exists():
            logger.info(f"[CompanyRegistry] Companies dir does not exist: {self._companies_dir}")
            return

        for company_dir in sorted(self._companies_dir.iterdir()):
            if not company_dir.is_dir():
                continue
            config_path = company_dir / "company.yaml"
            if not config_path.exists():
                continue

            try:
                _validate_company_id(company_dir.name)
                # Skip if a component is registered for this ID (component takes precedence)
                if company_dir.name in self._components:
                    logger.info(f"[CompanyRegistry] Skipping YAML for {company_dir.name} (component takes precedence)")
                    continue
                config = self._load_config(config_path, company_dir.name)
                self._configs[config.id] = config
                self._mtimes[config.id] = config_path.stat().st_mtime
                logger.info(f"[CompanyRegistry] Loaded company: {config.id} ({config.name})")
            except Exception as e:
                logger.error(f"[CompanyRegistry] Failed to load {company_dir.name}: {e}")

        # Load global bindings
        self._load_global_bindings()

        logger.info(
            f"[CompanyRegistry] Scan complete: {len(self._configs)} companies, "
            f"{len(self._global_bindings)} global bindings"
        )

    def _load_config(self, path: Path, expected_id: str) -> CompanyConfig:
        """Load and validate a company.yaml file."""
        import yaml

        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise CompanyError(f"Invalid company.yaml: expected dict, got {type(data).__name__}")

        # Ensure ID matches directory name
        data.setdefault("id", expected_id)
        if data["id"] != expected_id:
            raise CompanyError(
                f"Company ID mismatch: directory={expected_id}, config={data['id']}"
            )

        return CompanyConfig(**data)

    def _load_global_bindings(self) -> None:
        """Load global bindings from /data/companest/bindings.yaml."""
        self._global_bindings.clear()
        if not self._bindings_path.exists():
            self._bindings_mtime = 0.0
            return

        try:
            import yaml
            text = self._bindings_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        self._global_bindings.append(GlobalBinding(**item))
            self._bindings_mtime = self._bindings_path.stat().st_mtime
            logger.info(f"[CompanyRegistry] Loaded {len(self._global_bindings)} global bindings")
        except Exception as e:
            logger.error(f"[CompanyRegistry] Failed to load global bindings: {e}")

    def resolve(
        self,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> ResolveResult:
        """
        Resolve an incoming request to a company or global binding.

        Priority:
        1. Company bindings (first match wins)
        2. Global bindings (first match wins, sorted by priority desc)
        """
        # 1. Check company bindings
        for config in self._configs.values():
            if not config.enabled:
                continue
            for binding in config.bindings:
                if binding.matches(channel, chat_id, user_id):
                    return ResolveResult(company=config)

        # 2. Check global bindings
        sorted_bindings = sorted(self._global_bindings, key=lambda b: b.priority, reverse=True)
        for binding in sorted_bindings:
            if binding.matches(channel, chat_id, user_id):
                return ResolveResult(global_binding=binding)

        return ResolveResult()

    def get(self, company_id: str) -> Optional[CompanyConfig]:
        """Get a company config by ID."""
        return self._configs.get(company_id)

    def save(self, config: CompanyConfig) -> None:
        """Save a company config to disk (creates directory if needed)."""
        import yaml

        _validate_company_id(config.id)
        company_dir = self._companies_dir / config.id
        company_dir.mkdir(parents=True, exist_ok=True)

        config_path = company_dir / "company.yaml"
        data = config.model_dump(exclude_none=False)
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        config_path.write_text(text, encoding="utf-8")

        self._configs[config.id] = config
        self._mtimes[config.id] = config_path.stat().st_mtime
        logger.info(f"[CompanyRegistry] Saved company: {config.id}")

    def delete(self, company_id: str) -> bool:
        """Delete a company config and its directory."""
        import shutil

        _validate_company_id(company_id)
        company_dir = self._companies_dir / company_id
        if company_dir.exists():
            shutil.rmtree(company_dir)
        self._configs.pop(company_id, None)
        self._mtimes.pop(company_id, None)
        self._components.pop(company_id, None)
        logger.info(f"[CompanyRegistry] Deleted company: {company_id}")
        return True

    def reload(self) -> None:
        """Full reload  rescan all companies and bindings."""
        self.scan()

    def list_companies(self) -> List[str]:
        """List all company IDs."""
        return list(self._configs.keys())

    def list_enabled(self) -> List[CompanyConfig]:
        """List all enabled company configs."""
        return [c for c in self._configs.values() if c.enabled]

    def check_for_changes(self) -> bool:
        """Check if any company.yaml files have changed since last scan.

        Returns True if changes detected (caller should reload).
        """
        changed = False

        # Check existing companies for mtime changes
        if self._companies_dir.exists():
            current_dirs = set()
            for company_dir in self._companies_dir.iterdir():
                if not company_dir.is_dir():
                    continue
                config_path = company_dir / "company.yaml"
                if not config_path.exists():
                    continue
                cid = company_dir.name
                current_dirs.add(cid)
                try:
                    mtime = config_path.stat().st_mtime
                except OSError:
                    continue
                if cid not in self._mtimes or self._mtimes[cid] != mtime:
                    changed = True
                    break

            # Check for new or deleted companies
            if not changed:
                if current_dirs != set(self._mtimes.keys()):
                    changed = True

        # Check global bindings mtime
        if not changed and self._bindings_path.exists():
            try:
                mtime = self._bindings_path.stat().st_mtime
                if mtime != self._bindings_mtime:
                    changed = True
            except OSError:
                pass

        return changed

    def save_global_bindings(self, bindings: List[GlobalBinding]) -> None:
        """Save global bindings to disk."""
        import yaml

        self._data_dir.mkdir(parents=True, exist_ok=True)
        data = [b.model_dump(exclude_none=False) for b in bindings]
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._bindings_path.write_text(text, encoding="utf-8")
        self._global_bindings = bindings
        self._bindings_mtime = self._bindings_path.stat().st_mtime
        logger.info(f"[CompanyRegistry] Saved {len(bindings)} global bindings")

    def get_global_bindings(self) -> List[GlobalBinding]:
        """Return current global bindings."""
        return list(self._global_bindings)
