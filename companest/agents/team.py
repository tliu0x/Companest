import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .pi import Pi, PiConfig
from ..memory import MemoryManager
from .cascade import CascadeEngine
from ..exceptions import TeamError
from ..utils import run_sync
from ..modes import normalize_execution

if TYPE_CHECKING:
    from ..config import ProxyConfig

logger = logging.getLogger(__name__)


class TeamConfig:
    def __init__(
        self,
        id: str,
        role: str = "general",
        lead_pi: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: bool = True,
        always_on: bool = False,
        pis: Optional[List[PiConfig]] = None,
        rubric: Optional[List[Dict[str, Any]]] = None,
        cascade: bool = False,
        workflow: Optional[str] = None,
        cascade_cross_provider: bool = False,
        cascade_skip_models: Optional[List[str]] = None,
    ):
        self.id = id
        self.local_id: str = ""
        self.role = role
        self.lead_pi = lead_pi
        self.mode, self.cascade, self.workflow = normalize_execution(mode or "default", cascade, workflow)
        self.enabled = enabled
        self.always_on = always_on
        self.pis = pis or []
        self.rubric = rubric
        self.cascade_cross_provider = cascade_cross_provider
        self.cascade_skip_models = cascade_skip_models or []

    @classmethod
    def from_markdown(cls, path: Path) -> "TeamConfig":
        text = path.read_text(encoding="utf-8")
        team_id = path.parent.name

        role = _extract_field(text, "role", "general")
        lead_pi = _extract_field(text, "lead_pi")
        mode = _extract_field(text, "mode")
        enabled = _extract_field(text, "enabled", "true").lower() == "true"
        always_on = _extract_field(text, "always_on", "false").lower() == "true"

        rubric_raw = _extract_field(text, "rubric")
        rubric = _parse_rubric(rubric_raw) if rubric_raw else None

        cascade_cross_provider = _extract_field(text, "cascade_cross_provider", "false").lower() == "true"
        cascade_skip_raw = _extract_field(text, "cascade_skip_models")
        cascade_skip_models = (
            [m.strip() for m in cascade_skip_raw.split(",") if m.strip()]
            if cascade_skip_raw else []
        )

        pis = _parse_pi_sections(text)
        if not pis:
            pis_dir = path.parent / "pis"
            if pis_dir.exists():
                for pi_dir in sorted(pis_dir.iterdir()):
                    if pi_dir.is_dir():
                        pis.append(PiConfig(id=pi_dir.name))

        return cls(
            id=team_id,
            role=role,
            lead_pi=lead_pi,
            mode=mode,
            cascade=_extract_field(text, "cascade", "false").lower() == "true",
            workflow=_extract_field(text, "workflow"),
            enabled=enabled,
            always_on=always_on,
            pis=pis,
            rubric=rubric,
            cascade_cross_provider=cascade_cross_provider,
            cascade_skip_models=cascade_skip_models,
        )


class AgentTeam:
    def __init__(
        self,
        config: TeamConfig,
        memory: MemoryManager,
        proxy_config: Optional["ProxyConfig"] = None,
        tool_registry=None,
        cascade_engine: Optional[CascadeEngine] = None,
    ):
        self.id = config.id
        self.mode = config.mode
        self.cascade = config.cascade
        self.workflow = config.workflow
        self.rubric = config.rubric
        self.memory = memory
        self.pis: Dict[str, Pi] = {}

        team_cascade = cascade_engine
        if config.cascade_cross_provider or config.cascade_skip_models:
            team_cascade = CascadeEngine(
                cross_provider=config.cascade_cross_provider,
            )

        for pi_config in config.pis:
            pi = Pi(pi_config, memory, team_id=self.id, proxy_config=proxy_config,
                    tool_registry=tool_registry, cascade_engine=team_cascade)
            self.pis[pi_config.id] = pi

        self.lead_pi_id = config.lead_pi or (
            list(self.pis.keys())[0] if self.pis else None
        )

        logger.info(
            f"Team '{self.id}' initialized: "
            f"{len(self.pis)} pi(s), lead={self.lead_pi_id}"
        )

    def get_lead_config(self) -> Optional[PiConfig]:
        if self.lead_pi_id and self.lead_pi_id in self.pis:
            pi = self.pis[self.lead_pi_id]
            return PiConfig(id=pi.id, model=pi.model, tools=pi.tools_config, max_turns=pi.max_turns)
        return None


class TeamRegistry:
    def __init__(
        self,
        base_path: str,
        memory: MemoryManager,
        proxy_config: Optional["ProxyConfig"] = None,
        tool_registry=None,
        cascade_engine: Optional[CascadeEngine] = None,
    ):
        self.base_path = Path(base_path)
        self.memory = memory
        self.proxy_config = proxy_config
        self.tool_registry = tool_registry
        self.cascade_engine = cascade_engine
        self._configs: Dict[str, TeamConfig] = {}

    def scan_configs(self) -> None:
        self._configs.clear()
        teams_dir = self.base_path
        if not teams_dir.exists():
            logger.warning(f"Teams directory not found: {teams_dir}")
            return

        for team_dir in sorted(teams_dir.iterdir()):
            team_md = team_dir / "team.md"
            if team_dir.is_dir() and team_md.exists():
                try:
                    config = TeamConfig.from_markdown(team_md)
                    if config.enabled:
                        self._configs[config.id] = config
                except Exception as e:
                    logger.error(f"Failed to load team config {team_dir.name}: {e}")

        logger.info("Scanned %d team declarations", len(self._configs))

    def scan_company_teams(self, company_id: str, teams_dir: Path) -> None:
        self._configs.update(self._load_company_teams(company_id, teams_dir))

    async def scan_company_teams_async(self, company_id: str, teams_dir: Path) -> None:
        configs = await run_sync(self._load_company_teams, company_id, teams_dir)
        self._configs.update(configs)

    @staticmethod
    def _load_company_teams(company_id: str, teams_dir: Path) -> Dict[str, TeamConfig]:
        configs = {}
        if not teams_dir.exists():
            return configs
        for team_dir in sorted(teams_dir.iterdir()):
            team_md = team_dir / "team.md"
            if team_dir.is_dir() and team_md.exists():
                try:
                    config = TeamConfig.from_markdown(team_md)
                    config.local_id = config.id  # physical directory name
                    config.id = f"{company_id}/{config.id}"
                    if config.enabled:
                        configs[config.id] = config
                        logger.info(f"Company team loaded: {config.id}")
                except Exception as e:
                    logger.error(f"Failed to load company team {company_id}/{team_dir.name}: {e}")
        return configs

    def register(self, config: TeamConfig, always_on: bool = False) -> None:
        if not config.enabled:
            return
        self._configs[config.id] = config
        config.always_on = always_on or config.always_on

    def create(self, team_id: str) -> AgentTeam:
        """Construct a harness for this actor invocation; never cache instances."""
        config = self._configs.get(team_id)
        if config is None:
            raise TeamError(f"Team not found: {team_id}")
        return AgentTeam(config, self.memory, proxy_config=self.proxy_config,
                         tool_registry=self.tool_registry, cascade_engine=self.cascade_engine)

    def get_config(self, team_id: str) -> Optional[TeamConfig]:
        return self._configs.get(team_id)

    def get_configs_by_company(self, company_id: str) -> Dict[str, "TeamConfig"]:
        return {tid: cfg for tid, cfg in self._configs.items() if tid.startswith(f"{company_id}/")}

    def unregister_company(self, company_id: str) -> None:
        to_remove = [tid for tid in self._configs if tid.startswith(f"{company_id}/")]
        for tid in to_remove:
            self._configs.pop(tid, None)

    def list_teams(self) -> List[str]:
        return list(self._configs.keys())

    def list_active(self) -> List[str]:
        # Live execution belongs to Substrate; declarations are not running actors.
        return []

    def list_meta_teams(self) -> List[str]:
        return [tid for tid, cfg in self._configs.items() if cfg.always_on]

    def get_fleet_status(self) -> Dict:
        return {
            "registered": self.list_teams(),
            "active": self.list_active(),
            "meta": self.list_meta_teams(),
            "configs": {
                tid: {
                    "role": c.role,
                    "mode": c.mode,
                    "cascade": c.cascade,
                    "workflow": c.workflow,
                    "always_on": c.always_on,
                    "pi_count": len(c.pis),
                    "lead_pi": c.lead_pi,
                }
                for tid, c in self._configs.items()
            },
        }


def _parse_rubric(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse 'reasoning=0.4, depth=0.3, clarity=0.3' into normalized rubric list."""
    entries = []
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        name, val = part.split("=", 1)
        try:
            weight = float(val.strip())
        except ValueError:
            continue
        entries.append({"criterion": name.strip(), "weight": weight})

    if not entries:
        return None

    total = sum(e["weight"] for e in entries)
    if total > 0:
        for e in entries:
            e["weight"] = round(e["weight"] / total, 4)

    return entries


def _extract_field(text: str, field_name: str, default: str = None) -> Optional[str]:
    """Extract '- field_name: value' from markdown text."""
    pattern = rf"^-\s*{field_name}\s*:\s*(.+)$"
    match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return default


def _parse_pi_sections(text: str) -> List[PiConfig]:
    """Parse #### Pi: xxx sections from team.md."""
    pis = []
    sections = re.split(r"####\s+Pi:\s*(\S+)", text)
    for i in range(1, len(sections), 2):
        pi_id = sections[i].strip()
        body = sections[i + 1]

        model = _extract_field(body, "model", "deepseek-chat")
        tools_str = _extract_field(body, "tools")
        tools = [t.strip() for t in tools_str.split(",")] if tools_str else []
        max_turns = int(_extract_field(body, "max_turns", "10"))

        tools_deny_str = _extract_field(body, "tools_deny")
        tools_deny = [t.strip() for t in tools_deny_str.split(",")] if tools_deny_str else []

        pis.append(PiConfig(id=pi_id, model=model, tools=tools, tools_deny=tools_deny, max_turns=max_turns))

    return pis
