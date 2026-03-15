"""
Companest Agent Team + TeamRegistry

AgentTeam: A group of collaborating Pi agents with shared memory.
TeamRegistry: Dynamic team registry  scans .companest/teams/, on-demand instantiation.

Execution modes (default, cascade, loop, council, collaborative) live in
companest/modes/. AgentTeam methods are thin delegators for backward compat.

Teams are pure config (directories + markdown files).
Adding/removing a team = adding/removing a directory. Zero code changes.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

# Callback type: async fn(str) -> None
ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

from .pi import Pi, PiConfig
from .memory import MemoryManager
from .cascade import CascadeEngine, CascadeStrategy, AdequacyChecker
from .exceptions import TeamError

if TYPE_CHECKING:
    from .config import ProxyConfig

logger = logging.getLogger(__name__)


class TeamConfig:
    """
    Configuration for an Agent Team, parsed from team.md.

    Example team.md:
        # Team: stock
        - role: general
        - lead_pi: analyst
        - enabled: true
        - always_on: false

        #### Pi: analyst
        - model: deepseek-chat
        - tools: web_search, memory_read, memory_write
        - max_turns: 10
    """

    def __init__(
        self,
        id: str,
        role: str = "general",
        lead_pi: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: bool = True,
        always_on: bool = False,
        schedule: Optional[str] = None,
        pis: Optional[List[PiConfig]] = None,
        rubric: Optional[List[Dict[str, Any]]] = None,
        cascade_mode: Optional[str] = None,
        cascade_cross_provider: bool = False,
        cascade_skip_models: Optional[List[str]] = None,
    ):
        self.id = id
        self.local_id: str = ""
        self.role = role
        self.lead_pi = lead_pi
        self.mode = mode
        self.enabled = enabled
        self.always_on = always_on
        self.schedule = schedule
        self.pis = pis or []
        self.rubric = rubric
        self.cascade_mode = cascade_mode
        self.cascade_cross_provider = cascade_cross_provider
        self.cascade_skip_models = cascade_skip_models or []

    @classmethod
    def from_markdown(cls, path: Path) -> "TeamConfig":
        """Parse a team.md file into TeamConfig."""
        text = path.read_text(encoding="utf-8")
        team_id = path.parent.name

        # Parse team-level fields
        role = _extract_field(text, "role", "general")
        lead_pi = _extract_field(text, "lead_pi")
        mode = _extract_field(text, "mode")
        enabled = _extract_field(text, "enabled", "true").lower() == "true"
        always_on = _extract_field(text, "always_on", "false").lower() == "true"
        schedule = _extract_field(text, "schedule")

        # Parse rubric (e.g. "reasoning=0.4, depth=0.3, clarity=0.3")
        rubric_raw = _extract_field(text, "rubric")
        rubric = _parse_rubric(rubric_raw) if rubric_raw else None

        # Parse cascade overrides
        cascade_mode = _extract_field(text, "cascade_mode")
        cascade_cross_provider = _extract_field(text, "cascade_cross_provider", "false").lower() == "true"
        cascade_skip_raw = _extract_field(text, "cascade_skip_models")
        cascade_skip_models = (
            [m.strip() for m in cascade_skip_raw.split(",") if m.strip()]
            if cascade_skip_raw else []
        )

        # Parse Pi definitions
        pis = _parse_pi_sections(text)

        # If no pis defined in team.md, scan pis/ directory for soul.md files
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
            enabled=enabled,
            always_on=always_on,
            schedule=schedule,
            pis=pis,
            rubric=rubric,
            cascade_mode=cascade_mode,
            cascade_cross_provider=cascade_cross_provider,
            cascade_skip_models=cascade_skip_models,
        )


class AgentTeam:
    """
    A group of collaborating Pi agents = one department.

    Each team has:
    - Shared memory (via MemoryManager)
    - One or more Pi agents
    - A lead_pi that handles tasks by default
    """

    def __init__(
        self,
        config: TeamConfig,
        memory: MemoryManager,
        proxy_config: Optional["ProxyConfig"] = None,
        tool_registry=None,
        cascade_engine: Optional[CascadeEngine] = None,
    ):
        self.id = config.id
        self.role = config.role
        self.mode = config.mode or "default"
        self.enabled = config.enabled
        self.always_on = config.always_on
        self.rubric = config.rubric
        self.cascade_skip_models = config.cascade_skip_models
        self.memory = memory
        self.pis: Dict[str, Pi] = {}

        # Build per-team cascade engine override if team has cascade config
        team_cascade = cascade_engine
        if config.cascade_cross_provider or config.cascade_skip_models:
            team_cascade = CascadeEngine(
                cross_provider=config.cascade_cross_provider,
            )

        # Create Pi instances
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

    async def run(
        self, task: str, cascade: bool = False,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute task with lead Pi. Delegates to DefaultMode."""
        from .modes.default import DefaultMode
        return await DefaultMode(cascade=cascade).execute(
            self, task, on_progress=on_progress, user_context=user_context,
        )

    async def run_loop(
        self,
        task: str,
        max_subtasks: int = 10,
        cascade: bool = True,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Ralph Loop: decompose  iterate  synthesize. Delegates to LoopMode."""
        from .modes.loop import LoopMode
        return await LoopMode(max_subtasks=max_subtasks, cascade=cascade).execute(
            self, task, on_progress=on_progress, user_context=user_context,
        )

    async def run_council(
        self, task: str, on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Council mode: all Pis answer, lead synthesizes. Delegates to CouncilMode."""
        from .modes.council import CouncilMode
        return await CouncilMode().execute(
            self, task, on_progress=on_progress, user_context=user_context,
        )

    async def run_collaborative(
        self, task: str, pipeline: Optional[List[str]] = None,
        cascade: bool = False,
    ) -> str:
        """Multi-Pi pipeline. Delegates to CollaborativeMode."""
        from .modes.collaborative import CollaborativeMode
        return await CollaborativeMode(pipeline=pipeline, cascade=cascade).execute(
            self, task,
        )

    def get_lead_config(self) -> Optional[PiConfig]:
        """Get the lead Pi's config (used by CostGate for model info)."""
        if self.lead_pi_id and self.lead_pi_id in self.pis:
            pi = self.pis[self.lead_pi_id]
            return PiConfig(id=pi.id, model=pi.model, tools=pi.tools_config, max_turns=pi.max_turns)
        return None


class TeamRegistry:
    """
    Dynamic team registry  on-demand instantiation + hot reload.

    Scans .companest/teams/ directory. Each subdirectory = one team definition.
    Business teams are created on demand; meta-teams stay resident.
    """

    def __init__(
        self,
        base_path: str,
        memory: MemoryManager,
        idle_timeout: int = 300,
        proxy_config: Optional["ProxyConfig"] = None,
        tool_registry=None,
        cascade_engine: Optional[CascadeEngine] = None,
    ):
        self.base_path = Path(base_path)
        self.memory = memory
        self.idle_timeout = idle_timeout
        self.proxy_config = proxy_config
        self.tool_registry = tool_registry
        self.cascade_engine = cascade_engine

        # Configs (from directory scan)
        self._configs: Dict[str, TeamConfig] = {}

        # Active instances (on demand)
        self._instances: Dict[str, AgentTeam] = {}
        self._last_used: Dict[str, float] = {}
        self._active_count: Dict[str, int] = {}  # In-flight tasks per team

        # Meta-teams (always on)
        self._meta_teams: Dict[str, AgentTeam] = {}

        # Optional callback invoked after reload()  used by orchestrator
        # to invalidate SmartRouter cache when team list changes.
        self._on_reload_callback = None

    def set_reload_callback(self, callback) -> None:
        """Set a callback invoked after reload() completes."""
        self._on_reload_callback = callback

    def scan_configs(self) -> None:
        """Scan teams/ directory, load all team.md as configs."""
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
                        if config.always_on:
                            team = AgentTeam(config, self.memory, proxy_config=self.proxy_config,
                                            tool_registry=self.tool_registry,
                                            cascade_engine=self.cascade_engine)
                            self._meta_teams[config.id] = team
                            logger.info(f"Meta-team loaded: {config.id}")
                except Exception as e:
                    logger.error(f"Failed to load team config {team_dir.name}: {e}")

        logger.info(
            f"Scanned {len(self._configs)} team(s): "
            f"{len(self._meta_teams)} meta, "
            f"{len(self._configs) - len(self._meta_teams)} on-demand"
        )

    def scan_company_teams(self, company_id: str, teams_dir: Path) -> None:
        """Scan a company's private teams directory.

        Private teams are registered as {company_id}/{team_id}.
        Only the owning company can access them (enforced by orchestrator).
        """
        if not teams_dir.exists():
            return
        for team_dir in sorted(teams_dir.iterdir()):
            team_md = team_dir / "team.md"
            if team_dir.is_dir() and team_md.exists():
                try:
                    config = TeamConfig.from_markdown(team_md)
                    config.local_id = config.id  # physical directory name
                    config.id = f"{company_id}/{config.id}"
                    if config.enabled:
                        self._configs[config.id] = config
                        logger.info(f"Company team loaded: {config.id}")
                except Exception as e:
                    logger.error(f"Failed to load company team {company_id}/{team_dir.name}: {e}")

    def register(self, config: TeamConfig, always_on: bool = False) -> None:
        """Register a team programmatically (no filesystem needed)."""
        if not config.enabled:
            return
        self._configs[config.id] = config
        if always_on or config.always_on:
            team = AgentTeam(config, self.memory, proxy_config=self.proxy_config,
                             tool_registry=self.tool_registry,
                             cascade_engine=self.cascade_engine)
            self._meta_teams[config.id] = team
            logger.info(f"Registered meta-team: {config.id}")
        else:
            logger.info(f"Registered on-demand team: {config.id}")

    def get_or_create(self, team_id: str) -> AgentTeam:
        """Get team instance, creating on demand if needed."""
        # Check meta-teams first
        if team_id in self._meta_teams:
            return self._meta_teams[team_id]

        # Check hot cache
        if team_id in self._instances:
            self._last_used[team_id] = time.time()
            return self._instances[team_id]

        # Create on demand
        config = self._configs.get(team_id)
        if not config:
            raise TeamError(f"Team not found: {team_id}")

        team = AgentTeam(config, self.memory, proxy_config=self.proxy_config,
                         tool_registry=self.tool_registry,
                         cascade_engine=self.cascade_engine)
        self._instances[team_id] = team
        self._last_used[team_id] = time.time()
        logger.info(f"On-demand team created: {team_id}")
        return team

    def acquire(self, team_id: str) -> None:
        """Mark a team as having an active in-flight task."""
        self._active_count[team_id] = self._active_count.get(team_id, 0) + 1

    def release(self, team_id: str) -> None:
        """Mark a team task as completed."""
        count = self._active_count.get(team_id, 0)
        if count > 1:
            self._active_count[team_id] = count - 1
        else:
            self._active_count.pop(team_id, None)
        self._last_used[team_id] = time.time()

    def evict_idle(self) -> List[str]:
        """Release team instances that have been idle longer than timeout.

        Skips teams with active in-flight tasks to prevent data loss.
        """
        now = time.time()
        evicted = []
        for tid, ts in list(self._last_used.items()):
            if now - ts > self.idle_timeout:
                if self._active_count.get(tid, 0) > 0:
                    logger.debug(f"Skipping eviction of '{tid}': {self._active_count[tid]} task(s) active")
                    continue
                self._instances.pop(tid, None)
                self._last_used.pop(tid, None)
                self._active_count.pop(tid, None)
                evicted.append(tid)
                logger.info(f"Evicted idle team: {tid}")
        return evicted

    def reload(self) -> None:
        """Hot reload: clear idle instances, rescan configs.

        Instances with active in-flight tasks are preserved to prevent
        data loss. They will be cleaned up by eviction after tasks complete.
        """
        # Preserve instances with active tasks
        active_instances = {}
        active_last_used = {}
        active_counts = {}
        for tid in list(self._instances):
            if self._active_count.get(tid, 0) > 0:
                active_instances[tid] = self._instances[tid]
                active_last_used[tid] = self._last_used.get(tid, 0)
                active_counts[tid] = self._active_count[tid]
                logger.warning(
                    f"Preserving active instance '{tid}' during reload: "
                    f"{self._active_count[tid]} task(s) in-flight"
                )

        self._instances.clear()
        self._last_used.clear()
        self._meta_teams.clear()

        # Restore active instances
        self._instances.update(active_instances)
        self._last_used.update(active_last_used)
        # Keep all active counts (including meta teams)
        preserved_counts = {k: v for k, v in self._active_count.items() if v > 0}
        self._active_count.clear()
        self._active_count.update(preserved_counts)
        self._active_count.update(active_counts)

        self.scan_configs()
        self.memory.clear_cache()
        if self._on_reload_callback:
            self._on_reload_callback()
        logger.info("TeamRegistry reloaded")

    def get_meta_team(self, role: str) -> Optional[AgentTeam]:
        """Get a meta-team by role (e.g. 'cost_gate', 'memory_manager')."""
        for team in self._meta_teams.values():
            if team.role == role:
                return team
        return None

    def get_config(self, team_id: str) -> Optional[TeamConfig]:
        return self._configs.get(team_id)

    def get_configs_by_company(self, company_id: str) -> Dict[str, "TeamConfig"]:
        return {tid: cfg for tid, cfg in self._configs.items() if tid.startswith(f"{company_id}/")}

    def unregister_company(self, company_id: str) -> None:
        """Remove all teams belonging to a company.

        Configs are removed immediately so no new tasks can be routed.
        Instances with active in-flight tasks are kept alive and will be
        cleaned up by the eviction timer after tasks complete.
        """
        to_remove = [tid for tid in self._configs if tid.startswith(f"{company_id}/")]
        for tid in to_remove:
            self._configs.pop(tid, None)
            if self._active_count.get(tid, 0) > 0:
                logger.warning(
                    f"Deferring instance cleanup for '{tid}': "
                    f"{self._active_count[tid]} task(s) still active"
                )
                continue
            self._instances.pop(tid, None)
            self._last_used.pop(tid, None)
            self._active_count.pop(tid, None)
            self._meta_teams.pop(tid, None)

    def list_teams(self) -> List[str]:
        """All registered team IDs."""
        return list(self._configs.keys())

    def list_active(self) -> List[str]:
        """Currently instantiated team IDs."""
        return list(self._instances.keys()) + list(self._meta_teams.keys())

    def list_meta_teams(self) -> List[str]:
        return list(self._meta_teams.keys())

    def get_fleet_status(self) -> Dict:
        """Full status for API/dashboard."""
        return {
            "registered": self.list_teams(),
            "active": self.list_active(),
            "meta": self.list_meta_teams(),
            "configs": {
                tid: {
                    "role": c.role,
                    "mode": c.mode or "default",
                    "always_on": c.always_on,
                    "pi_count": len(c.pis),
                    "lead_pi": c.lead_pi,
                }
                for tid, c in self._configs.items()
            },
        }


#  Rubric parsing helper (used at config time) 

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

    # Normalize weights to sum to 1.0
    total = sum(e["weight"] for e in entries)
    if total > 0:
        for e in entries:
            e["weight"] = round(e["weight"] / total, 4)

    return entries


#  Markdown parsing helpers 

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
    # Find all Pi sections
    sections = re.split(r"####\s+Pi:\s*(\S+)", text)
    # sections = [preamble, pi_id1, pi_body1, pi_id2, pi_body2, ...]
    for i in range(1, len(sections), 2):
        pi_id = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""

        model = _extract_field(body, "model", "deepseek-chat")
        tools_str = _extract_field(body, "tools")
        tools = [t.strip() for t in tools_str.split(",")] if tools_str else []
        max_turns = int(_extract_field(body, "max_turns", "10"))

        tools_deny_str = _extract_field(body, "tools_deny")
        tools_deny = [t.strip() for t in tools_deny_str.split(",")] if tools_deny_str else []

        pis.append(PiConfig(id=pi_id, model=model, tools=tools, tools_deny=tools_deny, max_turns=max_turns))

    return pis
