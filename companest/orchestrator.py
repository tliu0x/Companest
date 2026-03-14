"""
Companest Orchestrator

The central coordinator for the Companest framework.
Routes tasks to Pi Agent Teams via the pipeline:

    SmartRouter.route() -CostGate -TeamRegistry -AgentTeam.run()

Usage:
    config = CompanestConfig.from_markdown(".companest/config.md")
    orchestrator = CompanestOrchestrator(config)
    orchestrator.init_teams(".companest")

    result, decision = await orchestrator.run_auto("Analyze TSLA")
    result = await orchestrator.run_team("Analyze TSLA", "stock")
"""

import importlib.util
import logging
import asyncio
from pathlib import Path
from typing import Callable, Coroutine, Dict, List, Any, Optional, Tuple

# Callback type: async fn(str) -> None
ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

from .config import CompanestConfig
from .memory import MemoryManager, Dreamer
from .team import TeamRegistry, AgentTeam
from .router import SmartRouter, RoutingDecision
from .cost_gate import CostGate, CostDecision, UserNotifier
from .cascade import CascadeEngine
from .modes import build_default_registry, ModeRegistry
from .litellm_client import LiteLLMClient
from .scheduler import Scheduler
# Lazy imports  these modules require optional deps (aiosqlite, boto3)
UserScheduler = None
ScheduledJob = None
MemoryArchiver = None
_USER_SCHEDULER_UNAVAILABLE = False

def _import_user_scheduler():
    global UserScheduler, ScheduledJob, _USER_SCHEDULER_UNAVAILABLE
    if UserScheduler is None:
        if _USER_SCHEDULER_UNAVAILABLE:
            return None
        missing = [
            dep for dep in ("aiosqlite", "apscheduler")
            if importlib.util.find_spec(dep) is None
        ]
        if missing:
            _USER_SCHEDULER_UNAVAILABLE = True
            logger.info(
                "UserScheduler disabled; missing optional dependencies: %s",
                ", ".join(missing),
            )
            return None
        try:
            from .user_scheduler import UserScheduler as _US, ScheduledJob as _SJ
            UserScheduler = _US
            ScheduledJob = _SJ
        except ImportError:
            _USER_SCHEDULER_UNAVAILABLE = True
            return None
    return UserScheduler

def _import_archiver():
    global MemoryArchiver
    if MemoryArchiver is None:
        try:
            from .archiver import MemoryArchiver as _MA
            MemoryArchiver = _MA
        except ImportError:
            pass
    return MemoryArchiver
from .events import EventBus, EventType
from .tools import ToolRegistry, ToolProvider, load_skills
from .multi_team import run_multi_team
from .background import BackgroundManager
from .company import CompanyRegistry, CompanyConfig, CompanyError
from .pi import Pi, PiConfig
from .workspace import WorkspaceRegistry
from .exceptions import (
    CompanestError,
    OrchestratorError,
    TeamError,
    CostGateError,
)

logger = logging.getLogger(__name__)


class CompanestOrchestrator:
    """
    Companest Orchestrator - Central coordinator for task execution.

    Primary entry point is run_team() which routes tasks to Pi Agent Teams.

    Example:
        config = CompanestConfig.from_markdown(".companest/config.md")
        orchestrator = CompanestOrchestrator(config)
        orchestrator.init_teams(".companest")

        result = await orchestrator.run_team("Analyze this stock", "stock")
    """

    def __init__(self, config: CompanestConfig):
        self.config = config
        self.events = EventBus()
        self.tool_registry = ToolRegistry()
        self._enrichment_cycles: Dict[str, dict] = {
            "research": {
                "interval": 1800,
                "prompt": (
                    "Run your scheduled intelligence gathering cycle. "
                    "Search for current news, market data, and tech trends. "
                    "Read the existing briefing.json first, then update it with fresh items. "
                    "Drop items older than 24 hours. Keep max 15 items."
                ),
            },
            "info-collection": {
                "interval": 300,
                "prompt": (
                    "Run your scheduled collection cycle. "
                    "Read watchlist.json for sources. "
                    "Fetch from each configured source (brave_search, reddit, hn, rss, x). "
                    "Read existing feed.json first, then merge new items. "
                    "Deduplicate by URL, drop items older than 2 hours, keep max 50 items. "
                    "If any significant trends emerge, update digest.json."
                ),
            },
        }
        self._info_refresh_team: str = "info-collection"
        self.mode_registry: ModeRegistry = build_default_registry(
            run_team_fn=self.run_team,
        )
        logger.info("Companest Orchestrator initialized")

    def on_event(self, event_type: EventType, callback) -> None:
        """Subscribe to a lifecycle event."""
        self.events.on(event_type, callback)

    async def register_tools(self, provider: ToolProvider) -> None:
        """Register a custom tool provider."""
        self.tool_registry.register(provider)
        await self.events.emit(EventType.TOOL_REGISTERED, {"provider": provider.name})

    def add_routing_binding(
        self, pattern: str, team_id: str, mode: str = "cascade",
    ) -> None:
        """Add a deterministic regex binding rule for fast routing.

        Bindings run before LLM routing -first match wins.
        Can be called before or after init_teams().
        """
        if hasattr(self, "_smart_router"):
            self._smart_router.add_binding(pattern, team_id, mode)
        else:
            if not hasattr(self, "_pending_bindings"):
                self._pending_bindings = []
            self._pending_bindings.append((pattern, team_id, mode))

    def register_enrichment_cycle(self, team_id: str, interval: int, prompt: str) -> None:
        """Register a custom enrichment cycle."""
        self._enrichment_cycles[team_id] = {"interval": interval, "prompt": prompt}

    def register_enrichment(self, source) -> None:
        """Register a custom enrichment source for system prompt injection."""
        if hasattr(self, "memory"):
            self.memory.register_enrichment(source)
        else:
            if not hasattr(self, "_pending_enrichments"):
                self._pending_enrichments = []
            self._pending_enrichments.append(source)

    async def register_team(self, config, always_on: bool = False) -> None:
        """Register a team. Works before or after init_teams()."""
        if hasattr(self, "team_registry"):
            self.team_registry.register(config, always_on=always_on)
            await self.events.emit(EventType.TEAM_REGISTERED, {"team_id": config.id})
        else:
            if not hasattr(self, "_pending_teams"):
                self._pending_teams = []
            self._pending_teams.append((config, always_on))

    def get_status(self) -> Dict[str, Any]:
        """Get current orchestrator status."""
        status = {
            "teams_initialized": hasattr(self, "team_registry"),
        }
        if hasattr(self, "team_registry"):
            status.update(self.get_teams_status())
        return status

    #  Pi Agent Team integration 

    def init_teams(
        self,
        base_path: str = ".companest",
        s3_bucket: Optional[str] = None,
        s3_region: str = "us-east-2",
    ) -> None:
        """
        Initialize the Pi Agent Team subsystem.

        Sets up MemoryManager, TeamRegistry, CostGate, Scheduler,
        and optionally MemoryArchiver for S3 backup.

        Args:
            base_path: Path to .companest/ directory
            s3_bucket: S3 bucket for memory backup (None = skip)
            s3_region: AWS region for S3
        """
        self.memory = MemoryManager(base_path)

        # Instantiate memory backend based on config.
        from .memory import FileBackend, QdrantBackend, MemorySearchService

        requested_backend = (self.config.memory_backend or "file").strip().lower()
        if requested_backend == "file":
            self.memory_backend = FileBackend(self.memory)
        elif requested_backend == "qdrant":
            mc = self.config.memory_config
            try:
                self.memory_backend = QdrantBackend(
                    manager=self.memory,
                    qdrant_url=mc.get("qdrant_url", "http://localhost:6333"),
                    qdrant_api_key=mc.get("qdrant_api_key"),
                    embedding_model=mc.get("embedding_model", "BAAI/bge-small-en-v1.5"),
                    prefer_grpc=mc.get("prefer_grpc", False),
                    in_memory=mc.get("in_memory", False),
                )
                logger.info("Qdrant memory backend initialized")
            except Exception as e:
                logger.warning(
                    "Failed to initialize Qdrant backend (%s); "
                    "falling back to file backend.", e,
                )
                self.memory_backend = FileBackend(self.memory)
        elif requested_backend == "viking":
            logger.warning(
                "Memory backend 'viking' is deprecated; use 'qdrant' instead. "
                "Falling back to file backend."
            )
            self.memory_backend = FileBackend(self.memory)
        else:
            logger.warning(
                "Unknown memory backend %r; falling back to file backend.",
                self.config.memory_backend,
            )
            self.memory_backend = FileBackend(self.memory)
        self.memory_search = MemorySearchService(self.memory_backend)

        # Make the backend available to tool factories via ToolRegistry
        self.tool_registry.memory_backend = self.memory_backend

        self.cascade_engine = CascadeEngine()
        self.team_registry = TeamRegistry(
            base_path=f"{base_path}/teams",
            memory=self.memory,
            proxy_config=self.config.proxy if self.config.proxy.enabled else None,
            tool_registry=self.tool_registry,
            cascade_engine=self.cascade_engine,
        )
        self.team_registry.scan_configs()

        # Load global skills from .companest/skills/
        skills = load_skills(base_path)
        for skill in skills.values():
            self.tool_registry.register_skill(skill)
        if skills:
            logger.info(f"Loaded {len(skills)} skill(s): {list(skills.keys())}")

        # Register external MCP servers from config
        for mcp_cfg in self.config.mcp_servers:
            self.tool_registry.register_external_mcp(mcp_cfg.name, mcp_cfg.to_sdk_config())
        if self.config.mcp_servers:
            logger.info(
                f"Registered {len(self.config.mcp_servers)} external MCP server(s): "
                f"{[s.name for s in self.config.mcp_servers]}"
            )

        # User-facing scheduler (APScheduler + SQLite)  optional dep
        _US = _import_user_scheduler()
        self.user_scheduler = _US(data_dir=Path(base_path)) if _US else None
        self._notification_callback = None

        # LiteLLM client for accurate spend tracking (optional)
        litellm_client = None
        if self.config.proxy.enabled and self.config.proxy.master_key:
            litellm_client = LiteLLMClient(
                base_url=self.config.proxy.base_url,
                master_key=self.config.proxy.master_key,
            )
            logger.info(f"LiteLLM client initialized: {self.config.proxy.base_url}")

        # Set proxy env vars once for SDKs that read them (claude-agent-sdk, openai-agents)
        if self.config.proxy.enabled:
            Pi.configure_proxy(self.config.proxy)

        self.cost_gate = CostGate(
            self.memory, notifier=UserNotifier(), litellm_client=litellm_client,
            event_bus=self.events,
        )
        self.scheduler = Scheduler()

        # S3 backup (optional)
        _MA = _import_archiver()
        if s3_bucket and _MA:
            self.archiver = _MA(
                memory=self.memory,
                bucket=s3_bucket,
                region=s3_region,
            )
            self.scheduler.add(
                "memory_backup",
                self.archiver.backup_snapshot,
                interval=14400,  # 4 hours
                run_on_start=False,
            )

        # Memory dreamer (importance scoring + compaction + GC)
        self.dreamer = Dreamer(
            memory=self.memory,
            proxy_config=self.config.proxy if self.config.proxy.enabled else None,
        )
        self.scheduler.add(
            "dream_nightly",
            self.dreamer.run_all_dreams,
            interval=86400,
            run_on_start=False,
        )
        self.scheduler.add(
            "dream_deep",
            self.dreamer.run_deep_consolidation,
            interval=604800,
            run_on_start=False,
        )

        # Background manager (eviction, reports, enrichment, job execution)
        self.background = BackgroundManager(
            run_team_fn=self.run_team,
            run_auto_fn=self.run_auto,
            team_registry=self.team_registry,
            cost_gate=self.cost_gate,
            events=self.events,
            scheduler=self.scheduler,
            user_scheduler=self.user_scheduler,
            enrichment_cycles=self._enrichment_cycles,
            info_refresh_team=self._info_refresh_team,
        )
        self.background.setup_schedules()
        # Wire user_scheduler execution callback so scheduled jobs actually run
        if self.user_scheduler is not None:
            self.background.set_execution_callback(
                self.background.execute_scheduled_job
            )

        # SmartRouter -LLM-powered team routing
        self._smart_router = SmartRouter(
            team_registry=self.team_registry,
            memory=self.memory,
            proxy_config=self.config.proxy if self.config.proxy.enabled else None,
            event_bus=self.events,
        )
        # Invalidate router cache when teams are reloaded
        self.team_registry.set_reload_callback(self._smart_router.invalidate_cache)

        # Flush any teams registered before init_teams()
        for config, ao in getattr(self, "_pending_teams", []):
            self.team_registry.register(config, always_on=ao)
        self._pending_teams = []

        # Flush any enrichments registered before init_teams()
        for source in getattr(self, "_pending_enrichments", []):
            self.memory.register_enrichment(source)
        self._pending_enrichments = []

        # Flush any routing bindings registered before init_teams()
        for pattern, team_id, mode in getattr(self, "_pending_bindings", []):
            self._smart_router.add_binding(pattern, team_id, mode)
        self._pending_bindings = []

        #  Workspace Registry (for coding teams) 
        self.workspace_registry = WorkspaceRegistry(base_path)
        self.workspace_registry.load()

        #  Evolution Engine + Canary Manager 
        from .evolution import EvolutionEngine
        from .canary import CanaryManager

        self.evolution = EvolutionEngine(
            memory=self.memory,
            event_bus=self.events,
        )
        self.canary = CanaryManager(
            memory=self.memory,
            memory_backend=self.memory_backend,
            event_bus=self.events,
        )

        # Schedule evolution checks (every 6 hours)
        self.scheduler.add(
            "evolution_cycle",
            self.evolution.run_cycle,
            interval=21600,
            run_on_start=False,
        )

        #  Company Registry + CEO Agents 
        self.company_registry = CompanyRegistry(base_path)
        self.company_registry.scan()
        self._ceo_pis: Dict[str, Pi] = {}

        for company in self.company_registry.list_enabled():
            try:
                self._init_company(company)
            except Exception as e:
                logger.error(f"Failed to init company {company.id}: {e}")

        # Periodic company config change detection
        self.scheduler.add(
            "company_watcher",
            self._check_company_changes,
            interval=30,
            run_on_start=False,
        )

        logger.info(
            f"Teams initialized: {len(self.team_registry.list_teams())} registered, "
            f"{len(self.team_registry.list_meta_teams())} meta, "
            f"{len(self._ceo_pis)} CEO agents"
        )

    #  Team Access Control 

    def _legacy_can_access_team(self, company_id: Optional[str], team_id: str) -> bool:
        """Check if a company is allowed to access a team.

        Rules:
        - Global teams (no '/') -accessible by all companies
        - Private teams (contains '/') -only the owner company can access
        - No company context -only global teams
        """
        if "/" not in team_id:
            # Global team — check shared_teams whitelist if company has one
            if company_id and hasattr(self, "company_registry"):
                company = self.company_registry.get(company_id)
                if company and company.shared_teams:
                    return team_id in company.shared_teams
            return True  # no whitelist or no company context
        owner = team_id.split("/", 1)[0]
        return company_id == owner

    def _get_company_shared_teams(self, company_id: Optional[str]) -> Optional[List[str]]:
        """Return the company's global-team whitelist.

        None means legacy unrestricted access. An empty list means no shared teams.
        """
        if not company_id or not hasattr(self, "company_registry"):
            return None
        company = self.company_registry.get(company_id)
        if company is None:
            return []
        return company.shared_teams

    def can_access_team(self, company_id: Optional[str], team_id: str) -> bool:
        """Check if a company is allowed to access a team.

        Rules:
        - Global teams: allowed if no whitelist (shared_teams is None),
          otherwise must be in the whitelist (empty list = no access).
        - Private teams (contains '/'): only the owner company can access.
        """
        if "/" not in team_id:
            shared_teams = self._get_company_shared_teams(company_id)
            if shared_teams is None:
                return True  # no whitelist configured — legacy unrestricted
            return team_id in shared_teams
        owner = team_id.split("/", 1)[0]
        return company_id == owner

    #  Company / CEO Management 

    def _init_company(self, company: CompanyConfig) -> None:
        """Initialize a single company: scan private teams, generate soul.md, create CEO Pi, register scheduler."""
        from .ceo_engine import build_cycle_prompt
        from .output import MemorySink
        from .component import CompanyComponent, CompanyContext, CompanyMemoryNamespace

        team_id = f"company-{company.id}"

        # Scan company private teams (e.g. /data/companest/companies/acme/teams/)
        company_teams_dir = Path(self.memory.base_path) / "companies" / company.id / "teams"
        if hasattr(self, "team_registry"):
            self.team_registry.scan_company_teams(company.id, company_teams_dir)

            # Register path overrides for company private teams
            for tid, cfg in self.team_registry.get_configs_by_company(company.id).items():
                if cfg.local_id:
                    actual_path = company_teams_dir / cfg.local_id
                    self.memory.register_team_path(tid, actual_path)

        # If this company has a registered component, invoke on_init()
        component = self.company_registry.get_component(company.id)
        if component is not None:
            ns = CompanyMemoryNamespace(self.memory, company.id)
            ctx = CompanyContext(
                company_id=company.id,
                memory=ns,
                tool_registry=self.tool_registry,
                event_bus=self.events,
                add_binding=lambda pat, tid, mode: self._smart_router.add_binding(
                    pat, tid, mode, owner_company_id=company.id
                ),
                register_enrichment=lambda src: (
                    setattr(src, 'company_id', company.id) or self.memory.register_enrichment(src)
                ),
            )
            try:
                component.on_init(ctx)
                logger.info(f"[Company] Component on_init() complete: {company.id}")
            except Exception as e:
                logger.error(f"[Company] Component on_init() failed for {company.id}: {e}")

        # Generate soul.md files (only if they don't exist)
        self._generate_ceo_soul(company)

        # Register default OutputSink (MemorySink -always on)
        if not hasattr(self, "_output_sinks"):
            self._output_sinks: Dict[str, list] = {}
        self._output_sinks.setdefault(company.id, [])
        if not any(isinstance(s, MemorySink) for s in self._output_sinks[company.id]):
            self._output_sinks[company.id].insert(0, MemorySink(self.memory))

        # Track cycle numbers per company
        if not hasattr(self, "_cycle_numbers"):
            self._cycle_numbers: Dict[str, int] = {}
        self._cycle_numbers.setdefault(company.id, 0)

        # Process routing_bindings from config
        for rb in company.routing_bindings:
            self._smart_router.add_binding(
                rb.get("pattern", ""), rb.get("team_id", ""),
                rb.get("mode", "cascade"),
                owner_company_id=company.id,
            )

        # Process memory_seed — only_if_missing semantics
        ns = CompanyMemoryNamespace(self.memory, company.id)
        seed = company.memory_seed
        if seed:
            for key, content in seed.get("shared", {}).items():
                if ns.read_shared(key) is None:
                    ns.write_shared(key, content)
            for team_id, team_seed in seed.get("teams", {}).items():
                for key, content in team_seed.items():
                    if ns.read_team_memory(team_id, key) is None:
                        ns.write_team_memory(team_id, key, content)

        # Process mcp_servers — company-scoped
        for mcp_cfg in company.mcp_servers:
            cfg = dict(mcp_cfg)
            name = cfg.pop("name", "")
            if name:
                self.tool_registry.register_company_mcp(company.id, name, cfg)

        # Validate shared_teams
        for st in company.shared_teams or []:
            if st not in self.team_registry.list_teams():
                logger.warning(f"Company {company.id} requests shared team '{st}' which does not exist")

        # Register CompanySchedules to Scheduler
        for sched in company.schedules:
            if not sched.enabled:
                continue
            task_name = f"company_{company.id}_{sched.name}"
            self.scheduler.add(
                task_name,
                lambda tid=sched.team_id, p=sched.prompt, m=sched.mode, cid=company.id:
                    self.run_team(p, tid, mode=m, user_context={"company_id": cid}),
                interval=sched.interval_seconds,
                run_on_start=False,
                scope_type="company",
                scope_id=company.id,
            )

        # Create CEO Pi
        if company.ceo.enabled:
            ceo_config = PiConfig(
                id="ceo",
                model=company.ceo.model,
                tools=["ceo"],  # "ceo" preset: run_team, run_auto, memory tools
                tools_deny=[],  # CEO has no deny list
                max_turns=company.ceo.max_turns,
            )

            ceo_pi = Pi(
                config=ceo_config,
                memory=self.memory,
                team_id=team_id,
                proxy_config=self.config.proxy if self.config.proxy.enabled else None,
                tool_registry=self.tool_registry,
                cascade_engine=self.cascade_engine,
            )

            # Inject orchestrator functions into CEO's tool context
            ceo_pi._extra_tool_context = {
                "run_team_fn": self.run_team,
                "run_auto_fn": self.run_auto,
                "company_id": company.id,
                "company_env": company.env,
                "company_shared_teams": company.shared_teams,
            }

            self._ceo_pis[company.id] = ceo_pi

            # Register CEO operating cycle in scheduler
            self.scheduler.add(
                f"ceo_{company.id}",
                lambda c=company, p=ceo_pi: self._run_ceo_cycle(c, p),
                interval=company.ceo.cycle_interval,
                run_on_start=False,
                scope_type="company",
                scope_id=company.id,
            )

            logger.info(
                f"[Company] CEO initialized: {company.id} "
                f"(model={company.ceo.model}, cycle={company.ceo.cycle_interval}s)"
            )

    async def apply_company(self, company_id: str) -> None:
        """Register or update a company — immediate effect, no watcher delay."""
        config = self.company_registry.get(company_id)
        if not config or not config.enabled:
            if self._is_company_initialized(company_id):
                await self.teardown_company(company_id)
            return
        # If already initialized, teardown first (clean update)
        if self._is_company_initialized(company_id):
            await self.teardown_company(company_id)
        self._init_company(config)

    async def teardown_company(self, company_id: str) -> None:
        """Full cleanup: component, scheduler, router, enrichment, teams, memory, MCP."""
        # 1. on_teardown
        comp = self.company_registry.get_component(company_id)
        if comp and hasattr(comp, 'on_teardown'):
            try:
                comp.on_teardown()
            except Exception as e:
                logger.error(f"[Company] on_teardown failed for {company_id}: {e}")

        # 2. Scheduler — remove by scope
        if hasattr(self, 'scheduler'):
            self.scheduler.remove_by_scope("company", company_id)
            # Also remove CEO schedule
            self.scheduler.remove(f"ceo_{company_id}")

        # 3. Router — remove by owner
        if hasattr(self, '_smart_router'):
            self._smart_router.remove_bindings_by_owner(company_id)

        # 4. Enrichment — remove from MemoryManager
        if hasattr(self, 'memory'):
            self.memory.remove_enrichments_by_company(company_id)

        # 5. Teams — unregister configs + instances + meta
        if hasattr(self, 'team_registry'):
            self.team_registry.unregister_company(company_id)

        # 6. Memory path overrides
        if hasattr(self, 'memory'):
            self.memory.unregister_company_paths(company_id)

        # 7. Company-scoped MCP
        if hasattr(self, 'tool_registry'):
            self.tool_registry.unregister_company_mcp(company_id)

        # 8. CEO Pi
        self._ceo_pis.pop(company_id, None)

        # 9. Output sinks
        if hasattr(self, '_output_sinks'):
            self._output_sinks.pop(company_id, None)

        logger.info(f"[Company] Teardown complete: {company_id}")

    def _is_company_initialized(self, company_id: str) -> bool:
        """Check if a company has been initialized."""
        if company_id in self._ceo_pis:
            return True
        if hasattr(self, 'team_registry'):
            if self.team_registry.get_configs_by_company(company_id):
                return True
        return False

    def _initialized_company_ids(self) -> List[str]:
        """Return company IDs that may still have runtime resources attached."""
        ids = set(self._ceo_pis.keys())
        if hasattr(self, "company_registry"):
            ids.update(self.company_registry.list_companies())
        if hasattr(self, "team_registry"):
            for team_id in self.team_registry.list_teams():
                if "/" in team_id:
                    ids.add(team_id.split("/", 1)[0])
        return sorted(ids)

    async def _run_ceo_cycle(self, company: CompanyConfig, ceo_pi: Pi) -> None:
        """Execute one CEO cycle and dispatch results through OutputSinks."""
        from .ceo_engine import build_cycle_prompt
        import time

        # Increment cycle number
        self._cycle_numbers[company.id] = self._cycle_numbers.get(company.id, 0) + 1
        cycle_number = self._cycle_numbers[company.id]

        # Build prompt: user override or structured engine
        if company.ceo.cycle_prompt:
            prompt = company.ceo.cycle_prompt
        else:
            available_teams = []
            if hasattr(self, "_smart_router"):
                available_teams = self._smart_router.get_available_teams(company.id)
            prompt = build_cycle_prompt(available_teams, cycle_number)

        # Execute
        result = await ceo_pi.run(
            prompt,
            cascade=True,
            user_context={
                "company_id": company.id,
                "company_context": company.domain,
            },
        )

        # Build cycle result
        cycle_result = {
            "cycle": cycle_number,
            "company_id": company.id,
            "result": result,
            "timestamp": time.time(),
            "summary": result[:500] if result else "",
        }

        # Dispatch to all output sinks
        for sink in self._output_sinks.get(company.id, []):
            try:
                await sink.emit(company.id, cycle_result)
            except Exception as e:
                logger.error(f"[OutputSink] Failed for {company.id}: {e}")

    def register_output_sink(self, company_id: str, sink) -> None:
        """Register an additional OutputSink for a company."""
        if not hasattr(self, "_output_sinks"):
            self._output_sinks = {}
        self._output_sinks.setdefault(company_id, [])
        self._output_sinks[company_id].append(sink)

    def _generate_ceo_soul(self, company: CompanyConfig) -> None:
        """Generate team and CEO soul.md files for a company (skip if exists)."""
        from .ceo_engine import generate_ceo_soul

        team_id = f"company-{company.id}"
        base = self.memory.base_path

        # Team soul.md
        team_soul_path = base / "teams" / team_id / "soul.md"
        if not team_soul_path.exists():
            team_soul_path.parent.mkdir(parents=True, exist_ok=True)
            team_soul_path.write_text(
                f"# {company.name} Operations Team\n\n"
                f"This team serves the {company.name} company.\n"
                f"Domain: {company.domain or 'General'}\n",
                encoding="utf-8",
            )

        # CEO soul.md -use ceo_engine for structured generation.
        # If a placeholder was created by CompanyBuilder.materialize() (contains
        # goals but no domain/teams/principles), regenerate the full version while
        # preserving any goals already written.
        ceo_soul_path = base / "teams" / team_id / "pis" / "ceo" / "soul.md"
        needs_generation = not ceo_soul_path.exists()
        existing_goals: List[str] = []
        if ceo_soul_path.exists():
            existing_text = ceo_soul_path.read_text(encoding="utf-8")
            # Detect placeholder: has goals section but no operational principles
            if "## Company Goals" in existing_text and "Operating Principles" not in existing_text:
                needs_generation = True
                # Preserve goals from the placeholder
                for line in existing_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- ") and "## " not in stripped:
                        existing_goals.append(stripped[2:])
        if needs_generation:
            ceo_soul_path.parent.mkdir(parents=True, exist_ok=True)
            available_teams = []
            if hasattr(self, "_smart_router"):
                available_teams = self._smart_router.get_available_teams(company.id)
            # Merge: config goals take precedence, then any existing placeholder goals
            merged_goals = list(company.ceo.goals) if company.ceo.goals else []
            for g in existing_goals:
                if g not in merged_goals:
                    merged_goals.append(g)
            soul_content = generate_ceo_soul(
                company_name=company.name,
                domain=company.domain,
                goals=merged_goals or None,
                kpis=company.ceo.kpis or None,
                available_teams=available_teams or None,
            )
            ceo_soul_path.write_text(soul_content, encoding="utf-8")

        # Memory directory
        mem_dir = base / "teams" / team_id / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        # Team.md config (minimal, so TeamRegistry can discover it)
        team_md_path = base / "teams" / team_id / "team.md"
        if not team_md_path.exists():
            team_md_path.write_text(
                f"# Team: {team_id}\n"
                f"- role: company\n"
                f"- lead_pi: ceo\n\n"
                f"## Pis\n"
                f"### ceo\n"
                f"- model: {company.ceo.model}\n"
                f"- tools: ceo\n"
                f"- tools_deny: none\n",
                encoding="utf-8",
            )

    async def _check_company_changes(self) -> None:
        """Periodic check for company config changes (called by Scheduler)."""
        if not hasattr(self, "company_registry"):
            return
        if self.company_registry.check_for_changes():
            logger.info("[Company] Config changes detected, reloading...")
            old_ids = set(self._initialized_company_ids())

            # Full teardown of all previously initialized companies
            for cid in old_ids:
                try:
                    if self._is_company_initialized(cid):
                        await self.teardown_company(cid)
                except Exception as e:
                    logger.error(f"Failed to teardown company {cid}: {e}")

            self.company_registry.reload()

            # Re-init all enabled companies
            new_ids = set()
            for company in self.company_registry.list_enabled():
                new_ids.add(company.id)
                try:
                    self._init_company(company)
                except Exception as e:
                    logger.error(f"Failed to re-init company {company.id}: {e}")

            # Teardown companies that were removed (existed before but not after reload)
            removed = old_ids - new_ids
            for cid in removed:
                logger.info(f"[Company] Company {cid} removed, already torn down")

            # Invalidate memory/prompt caches for company teams
            self.memory.clear_cache()

    def set_notification_callback(self, callback) -> None:
        """Set the notification callback (called by cli.py after MasterConnection is ready).

        Args:
            callback: async (chat_id, channel, user_id, message) -> None
                      (i.e. MasterConnection.send_notification)
        """
        self._notification_callback = callback
        if hasattr(self, "background"):
            self.background.set_notification_callback(callback)

        # Wire cost_gate notifier -system alerts go to admin channel
        if hasattr(self, "cost_gate") and self.cost_gate.notifier:
            async def _cost_alert(msg: str) -> None:
                await callback("admin", "system", "companest", msg)
            self.cost_gate.notifier.send_fn = _cost_alert

    async def run_team(
        self,
        task: str,
        team_id: str,
        skip_cost_check: bool = False,
        mode: Optional[str] = None,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
    ) -> str:
        """
        Run a task via a specific Pi Agent Team.

        Flow: CostGate evaluate -Team.run() -record spending.

        Args:
            task: The task/prompt
            team_id: Target team ID
            skip_cost_check: Skip CostGate (for meta-team internal calls)
            mode: Execution mode override. None = use team.md default or "default"
            on_progress: Optional async callback for progress updates
            priority: Task priority for cost gate -"critical", "high", "normal", "low"

        Returns:
            Result string from the team's lead Pi

        Raises:
            OrchestratorError: If cost rejected or team fails
        """
        if not hasattr(self, "team_registry"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

        # Resolve company from user_context and inject company info
        user_context = dict(user_context) if user_context else {}
        company_id = user_context.get("company_id")
        company_budget_hourly = None
        if not company_id and hasattr(self, "company_registry"):
            result = self.company_registry.resolve(
                user_context.get("channel"),
                user_context.get("chat_id"),
                user_context.get("user_id"),
            )
            if result.company:
                company_id = result.company.id
                user_context["company_id"] = company_id
                if not user_context.get("company_context"):
                    user_context["company_context"] = result.company.domain
                company_budget_hourly = result.company.preferences.budget_hourly_usd

        if company_id and not company_budget_hourly and hasattr(self, "company_registry"):
            company = self.company_registry.get(company_id)
            if company:
                company_budget_hourly = company.preferences.budget_hourly_usd

        # Hard access control: private teams only accessible by owner
        if not self.can_access_team(company_id, team_id):
            raise OrchestratorError(
                f"Access denied: company '{company_id}' cannot access team '{team_id}'"
            )

        # Get team (on-demand creation for business teams)
        try:
            team = self.team_registry.get_or_create(team_id)
        except TeamError as e:
            raise OrchestratorError(f"Team routing failed: {e}")
        effective_mode = mode if mode is not None else getattr(team, "mode", "default")

        # Cost gate evaluation
        if not skip_cost_check and hasattr(self, "cost_gate"):
            lead_config = team.get_lead_config()
            model = lead_config.model if lead_config else "claude-sonnet-4-5-20250929"

            decision = await self.cost_gate.evaluate(
                task, team_id, model, priority=priority,
                company_id=company_id,
                company_budget_hourly=company_budget_hourly,
            )

            if decision.action == "rejected":
                raise OrchestratorError(
                    f"CostGate rejected: {decision.reason}",
                    details={
                        "team": team_id,
                        "model": model,
                        "estimated_cost": decision.estimate.estimated_cost_usd,
                    },
                )

            if decision.action in ("auto_approve", "notify_approve"):
                logger.info(
                    f"[CostGate] {decision.action}: "
                    f"${decision.estimate.estimated_cost_usd:.4f} for {team_id}"
                )
                if on_progress:
                    await on_progress(
                        f"\U0001f4b0 ${decision.estimate.estimated_cost_usd:.4f} approved"
                    )

        # Inject user_scheduler into Pi instances for scheduling tools
        if hasattr(self, "user_scheduler"):
            for pi in team.pis.values():
                pi.user_scheduler = self.user_scheduler

        # Inject workspace context into coding team Pi instances
        workspace_id = user_context.get("workspace_id")
        if workspace_id and hasattr(self, "workspace_registry"):
            ws = self.workspace_registry.get(workspace_id)
            if ws:
                ws_context = self.workspace_registry.build_context(workspace_id)
                for pi in team.pis.values():
                    extra = getattr(pi, "_extra_tool_context", {})
                    extra["workspace_path"] = ws.path
                    extra["workspace_context"] = ws_context
                    pi._extra_tool_context = extra

        company_shared_teams = self._get_company_shared_teams(company_id)
        for pi in team.pis.values():
            extra = getattr(pi, "_extra_tool_context", {})
            extra["company_shared_teams"] = company_shared_teams
            pi._extra_tool_context = extra

        # Emit TASK_STARTED
        event_data = {"team_id": team_id, "mode": effective_mode, "task_preview": task[:200]}
        if company_id:
            event_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_STARTED, event_data)

        # Execute with acquire/release for eviction safety
        self.team_registry.acquire(team_id)
        try:
            try:
                execution_mode = self.mode_registry.get(effective_mode)
            except KeyError:
                raise OrchestratorError(
                    f"Unknown execution mode: '{effective_mode}'",
                    details={"available": self.mode_registry.list_modes()},
                )
            result = await execution_mode.execute(
                team, task, on_progress=on_progress, user_context=user_context,
            )
        except OrchestratorError:
            raise
        except Exception as e:
            await self.events.emit(EventType.TASK_FAILED, {
                "team_id": team_id, "mode": effective_mode, "error": str(e),
            })
            raise OrchestratorError(
                f"Team '{team_id}' execution failed: {e}",
                details={"team": team_id, "mode": effective_mode},
            )
        finally:
            self.team_registry.release(team_id)

        # Emit TASK_COMPLETED
        completed_data = {"team_id": team_id, "mode": effective_mode}
        if company_id:
            completed_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_COMPLETED, completed_data)

        # Record estimated spend (actual tokens unknown without proxy)
        if hasattr(self, "cost_gate") and not skip_cost_check:
            try:
                lead_config = team.get_lead_config()
                model = lead_config.model if lead_config else "claude-sonnet-4-5-20250929"
                estimate = self.cost_gate.estimate_cost(task, model, team_id)
                self.cost_gate.record_spending(
                    team_id=team_id,
                    task=task,
                    tokens={
                        "input": estimate.estimated_input_tokens,
                        "output": estimate.estimated_output_tokens,
                    },
                    cost=estimate.estimated_cost_usd,
                    company_id=company_id,
                )
            except Exception as e:
                logger.warning(f"Failed to record spending for {team_id}: {e}")

        return result

    async def run_auto(
        self,
        task: str,
        mode: Optional[str] = None,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, RoutingDecision]:
        """
        Auto-route a task to the best team(s) using SmartRouter.

        Uses LLM-powered routing (Haiku ~$0.001) with keyword fallback.
        Supports multi-team execution (sequential/parallel) for cross-domain tasks.

        Args:
            task: The task/prompt
            mode: Execution mode override. None = use router's decision.
            on_progress: Optional async callback for progress updates

        Returns:
            (result_string, routing_decision)

        Raises:
            OrchestratorError: If declined, not initialized, or execution fails
        """
        if not hasattr(self, "_smart_router"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

        # Resolve company/global binding from user_context
        user_context = dict(user_context) if user_context else {}
        preferred_teams = None
        if "company_id" not in user_context and hasattr(self, "company_registry"):
            resolve_result = self.company_registry.resolve(
                user_context.get("channel"),
                user_context.get("chat_id"),
                user_context.get("user_id"),
            )
            if resolve_result.company:
                user_context["company_id"] = resolve_result.company.id
                if not user_context.get("company_context"):
                    user_context["company_context"] = resolve_result.company.domain
                if resolve_result.company.preferences.preferred_teams:
                    preferred_teams = resolve_result.company.preferences.preferred_teams
            elif resolve_result.global_binding:
                # Global binding -route directly to team, skip SmartRouter
                gb = resolve_result.global_binding
                effective_mode = mode if mode is not None else gb.mode
                from .router import TeamAssignment
                result_str = await self.run_team(
                    task, gb.team_id, mode=effective_mode,
                    on_progress=on_progress, user_context=user_context,
                )
                decision = RoutingDecision(
                    teams=[TeamAssignment(team_id=gb.team_id, instruction=task)],
                    strategy="single",
                    reasoning="Global binding match",
                    confidence=1.0,
                    mode=effective_mode,
                )
                return result_str, decision
            elif user_context.get("binding_team_id"):
                # Gateway sent a legacy binding hint -honour it as a fallback
                hint_team = user_context["binding_team_id"]
                hint_mode = user_context.get("binding_mode", "default")
                effective_mode = mode if mode is not None else hint_mode
                from .router import TeamAssignment
                result_str = await self.run_team(
                    task, hint_team, mode=effective_mode,
                    on_progress=on_progress, user_context=user_context,
                )
                decision = RoutingDecision(
                    teams=[TeamAssignment(team_id=hint_team, instruction=task)],
                    strategy="single",
                    reasoning="Gateway binding hint (legacy)",
                    confidence=1.0,
                    mode=effective_mode,
                )
                return result_str, decision

        # If company was already in user_context, try to get preferred_teams
        if preferred_teams is None and user_context.get("company_id") and hasattr(self, "company_registry"):
            company = self.company_registry.get(user_context["company_id"])
            if company and company.preferences.preferred_teams:
                preferred_teams = company.preferences.preferred_teams

        cid = user_context.get("company_id")
        shared_teams = self._get_company_shared_teams(cid)

        decision = await self._smart_router.route(
            task, preferred_teams=preferred_teams,
            company_id=user_context.get("company_id"),
            shared_teams=shared_teams,
        )
        # Note: SmartRouter.route() already emits TASK_ROUTED via _emit_routing_audit()

        if decision.declined:
            raise OrchestratorError(
                f"No suitable team found: {decision.decline_reason or 'task does not match any team'}",
                details={"declined": True, "reasoning": decision.reasoning},
            )

        effective_mode = mode if mode is not None else decision.mode

        # Emit routing progress
        if on_progress:
            teams_str = "+".join(t.team_id for t in decision.teams)
            await on_progress(
                f"\U0001f50d \u2192 {teams_str} ({effective_mode}, {decision.confidence:.0%})"
            )

        # On-demand info refresh: ask info team to do a quick
        # targeted fetch before the business team runs.
        # Skip if the task is routed to the info team itself.
        routed_ids = {t.team_id for t in decision.teams}
        if self._info_refresh_team not in routed_ids:
            if on_progress:
                await on_progress("\U0001f4e1 Refreshing info feeds...")
            await self.background.refresh_info_for_task(task)

        if len(decision.teams) == 1:
            # Single team -use existing run_team()
            assignment = decision.teams[0]
            result = await self.run_team(
                assignment.instruction or task,
                assignment.team_id,
                mode=effective_mode,
                on_progress=on_progress,
                user_context=user_context,
                priority=decision.task_priority,
            )
            return result, decision

        # Multi-team execution
        result = await run_multi_team(
            self.run_team, decision, mode=effective_mode,
            on_progress=on_progress, user_context=user_context,
        )
        return result, decision

    def get_teams_status(self) -> Dict[str, Any]:
        """Get team subsystem status."""
        status = {}
        if hasattr(self, "team_registry"):
            status["teams"] = self.team_registry.get_fleet_status()
        if hasattr(self, "cost_gate"):
            status["finance"] = self.cost_gate.get_spending_summary()
        if hasattr(self, "scheduler"):
            status["scheduler"] = self.scheduler.get_status()
        return status

    @classmethod
    def from_config_file(cls, path: str) -> "CompanestOrchestrator":
        """Create an orchestrator from a config file."""
        if path.endswith(".md"):
            config = CompanestConfig.from_markdown(path)
        else:
            config = CompanestConfig.from_json_file(path)

        return cls(config)

    @classmethod
    def discover_and_create(cls, base_path: str = ".") -> Optional["CompanestOrchestrator"]:
        """Discover config and create orchestrator."""
        config = CompanestConfig.discover_config(base_path)
        if config:
            return cls(config)
        return None
