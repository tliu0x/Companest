import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from ..exceptions import CompanyError

logger = logging.getLogger(__name__)


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


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


class CompanyBinding(BaseModel):
    """Identifies which incoming requests belong to this company."""
    channel: Optional[str] = None    # "telegram", "api", None=wildcard
    chat_id: Optional[str] = None
    user_id: Optional[str] = None

    def matches(self, channel: Optional[str], chat_id: Optional[str], user_id: Optional[str]) -> bool:
        if self.channel is not None and self.channel != channel:
            return False
        if self.chat_id is not None and self.chat_id != chat_id:
            return False
        if self.user_id is not None and self.user_id != user_id:
            return False
        return self.channel is not None or self.chat_id is not None or self.user_id is not None


class GlobalBinding(CompanyBinding):
    """Static routing rule  route a chat directly to a team (not via company)."""
    team_id: str
    mode: str = "cascade"
    priority: int = 0


class CompanySchedule(BaseModel):
    name: str
    team_id: str
    prompt: str
    interval_seconds: int = 1800
    mode: str = "cascade"
    enabled: bool = True


class CompanyCEOConfig(BaseModel):
    model: str = "deepseek-chat"
    max_turns: int = 50
    cycle_interval: int = 1800  # seconds
    cycle_prompt: Optional[str] = None  # replaces the ceo_engine-generated prompt when set
    goals: List[str] = Field(default_factory=list)
    kpis: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class CompanyPreferences(BaseModel):
    default_mode: str = "cascade"
    preferred_teams: List[str] = Field(default_factory=list)
    budget_hourly_usd: float = 1.0
    budget_monthly_usd: float = 200.0


class CompanyConfig(BaseModel):
    id: str
    name: str
    domain: str = ""  # injected into system prompts as company context
    bindings: List[CompanyBinding] = Field(default_factory=list)
    preferences: CompanyPreferences = Field(default_factory=CompanyPreferences)
    ceo: CompanyCEOConfig = Field(default_factory=CompanyCEOConfig)
    schedules: List[CompanySchedule] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    shared_teams: Optional[List[str]] = None
    routing_bindings: List[Dict[str, str]] = Field(default_factory=list)
    memory_seed: Dict[str, Any] = Field(default_factory=dict)
    mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _validate_company_id(v)


class ResolveResult:
    __slots__ = ("company", "global_binding")

    def __init__(
        self,
        company: Optional[CompanyConfig] = None,
        global_binding: Optional[GlobalBinding] = None,
    ):
        self.company = company
        self.global_binding = global_binding


class CompanyRegistry:
    def __init__(self, data_dir: str):
        self._data_dir = Path(data_dir)
        self._companies_dir = self._data_dir / "companies"
        self._bindings_path = self._data_dir / "bindings.yaml"
        self._configs: Dict[str, CompanyConfig] = {}
        self._global_bindings: List[GlobalBinding] = []
        self._mtimes: Dict[str, float] = {}
        self._bindings_mtime: float = 0.0
        self._components: Dict[str, Any] = {}  # company_id -> CompanyComponent

    def register_component(self, component: Any) -> None:
        cid = component.company_id
        self._configs[cid] = component.config
        self._components[cid] = component
        logger.info(f"[CompanyRegistry] Registered component: {cid}")

    def get_component(self, company_id: str) -> Optional[Any]:
        return self._components.get(company_id)

    def list_components(self) -> List[str]:
        return list(self._components.keys())

    def scan(self) -> None:
        self._configs.clear()
        self._mtimes.clear()

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
                if company_dir.name in self._components:
                    logger.info(f"[CompanyRegistry] Skipping YAML for {company_dir.name} (component takes precedence)")
                    continue
                config = self._load_config(config_path, company_dir.name)
                self._configs[config.id] = config
                self._mtimes[config.id] = config_path.stat().st_mtime
                logger.info(f"[CompanyRegistry] Loaded company: {config.id} ({config.name})")
            except Exception as e:
                logger.error(f"[CompanyRegistry] Failed to load {company_dir.name}: {e}")

        self._load_global_bindings()

        logger.info(
            f"[CompanyRegistry] Scan complete: {len(self._configs)} companies, "
            f"{len(self._global_bindings)} global bindings"
        )

    def _load_config(self, path: Path, expected_id: str) -> CompanyConfig:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise CompanyError(f"Invalid company.yaml: expected dict, got {type(data).__name__}")

        data.setdefault("id", expected_id)
        if data["id"] != expected_id:
            raise CompanyError(
                f"Company ID mismatch: directory={expected_id}, config={data['id']}"
            )

        return CompanyConfig(**data)

    def _load_global_bindings(self) -> None:
        self._global_bindings.clear()
        if not self._bindings_path.exists():
            self._bindings_mtime = 0.0
            return

        try:
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
        for config in self._configs.values():
            if not config.enabled:
                continue
            for binding in config.bindings:
                if binding.matches(channel, chat_id, user_id):
                    return ResolveResult(company=config)

        sorted_bindings = sorted(self._global_bindings, key=lambda b: b.priority, reverse=True)
        for binding in sorted_bindings:
            if binding.matches(channel, chat_id, user_id):
                return ResolveResult(global_binding=binding)

        return ResolveResult()

    def get(self, company_id: str) -> Optional[CompanyConfig]:
        return self._configs.get(company_id)

    def save(self, config: CompanyConfig) -> None:
        _validate_company_id(config.id)
        company_dir = self._companies_dir / config.id
        company_dir.mkdir(parents=True, exist_ok=True)

        config_path = company_dir / "company.yaml"
        data = config.model_dump(exclude_none=False)
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _atomic_write_text(config_path, text)

        self._configs[config.id] = config
        self._mtimes[config.id] = config_path.stat().st_mtime
        logger.info(f"[CompanyRegistry] Saved company: {config.id}")

    def delete(self, company_id: str) -> bool:
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
        self.scan()

    def list_companies(self) -> List[str]:
        return list(self._configs.keys())

    def list_enabled(self) -> List[CompanyConfig]:
        return [c for c in self._configs.values() if c.enabled]

    def check_for_changes(self) -> bool:
        changed = False

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

            if not changed:
                if current_dirs != set(self._mtimes.keys()):
                    changed = True

        if not changed and self._bindings_path.exists():
            try:
                mtime = self._bindings_path.stat().st_mtime
                if mtime != self._bindings_mtime:
                    changed = True
            except OSError:
                pass

        return changed

    def save_global_bindings(self, bindings: List[GlobalBinding]) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        data = [b.model_dump(exclude_none=False) for b in bindings]
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _atomic_write_text(self._bindings_path, text)
        self._global_bindings = bindings
        self._bindings_mtime = self._bindings_path.stat().st_mtime
        logger.info(f"[CompanyRegistry] Saved {len(bindings)} global bindings")

    def get_global_bindings(self) -> List[GlobalBinding]:
        return list(self._global_bindings)
