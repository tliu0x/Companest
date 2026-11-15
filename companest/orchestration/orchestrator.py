import asyncio
import importlib.util
import logging
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Union

from ..utils import run_sync, finish_on_cancel
from ..config import CompanestConfig
from ..memory import MemoryManager, Dreamer, EnrichmentSource, FileBackend, QdrantBackend
from ..agents.team import TeamConfig, TeamRegistry
from .router import SmartRouter, RoutingDecision, TeamAssignment
from .cost_gate import CostGate, UserNotifier
from ..agents.cascade import CascadeEngine
from ..modes import build_default_registry, ModeRegistry
from ..jobs.scheduler import Scheduler
from ..memory.archiver import MemoryArchiver
from ..events import EventBus, EventCallback, EventType
from ..agents.tools import ToolProvider, ToolRegistry, load_skills
from .multi_team import run_multi_team, ProgressCallback
from .background import BackgroundManager
from ..companies.company import CompanyRegistry, CompanyConfig
from ..companies.component import (CompanyContext, CompanyMemoryNamespace,
                                   LoopBoundRegistry, bind_to_loop, run_component_hook)
from ..companies.ceo_engine import build_cycle_prompt, generate_ceo_soul
from ..companies.output import MemorySink, OutputSink
from ..agents.pi import Pi, PiConfig, _run_tool_context
from ..agents.workspace import WorkspaceRegistry
from ..exceptions import OrchestratorError, TeamError

logger = logging.getLogger(__name__)

_USER_SCHEDULER_UNAVAILABLE = False


def _import_user_scheduler():
    """Return UserScheduler, or None when the optional scheduler extra is not installed."""
    global _USER_SCHEDULER_UNAVAILABLE
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
    from ..jobs.user_scheduler import UserScheduler
    return UserScheduler


class CompanestOrchestrator:
    def __init__(self, config: CompanestConfig):
        self.config = config
        self._company_lock = asyncio.Lock()
        self._company_owner = None
        self._initialized_components = set()
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
        self._output_sinks: Dict[str, List[OutputSink]] = {}
        self._pending_teams: List[Tuple[TeamConfig, bool]] = []
        self._pending_enrichments: List[EnrichmentSource] = []
        self._pending_bindings: List[Tuple[str, str, str]] = []
        self._cycle_numbers: Dict[str, int] = {}
        self.mode_registry: ModeRegistry = build_default_registry(
            run_team_fn=self.run_team,
        )
        logger.info("Companest Orchestrator initialized")

    def on_event(self, event_type: EventType, callback: EventCallback) -> None:
        self.events.on(event_type, callback)

    async def register_tools(self, provider: ToolProvider) -> None:
        self.tool_registry.register(provider)
        await self.events.emit(EventType.TOOL_REGISTERED, {"provider": provider.name})

    async def register_team(self, config: TeamConfig, always_on: bool = False) -> None:
        if hasattr(self, "team_registry"):
            self.team_registry.register(config, always_on=always_on)
            await self.events.emit(EventType.TEAM_REGISTERED, {"team_id": config.id})
        else:
            self._pending_teams.append((config, always_on))

    def register_enrichment(self, source: EnrichmentSource) -> None:
        if hasattr(self, "memory"):
            self.memory.register_enrichment(source)
        else:
            self._pending_enrichments.append(source)

    def register_enrichment_cycle(self, team_id: str, interval: int, prompt: str) -> None:
        self._enrichment_cycles[team_id] = {"interval": interval, "prompt": prompt}

    def add_routing_binding(self, pattern: str, team_id: str, mode: str = "cascade") -> None:
        if hasattr(self, "_smart_router"):
            self._smart_router.add_binding(pattern, team_id, mode)
        else:
            self._pending_bindings.append((pattern, team_id, mode))

    def register_output_sink(self, company_id: str, sink: OutputSink) -> None:
        self._output_sinks.setdefault(company_id, []).append(sink)

    @classmethod
    def from_config_file(cls, path: Union[str, Path]) -> "CompanestOrchestrator":
        path = Path(path)
        config = CompanestConfig.from_markdown(path) if path.suffix == ".md" else CompanestConfig.from_json_file(path)
        return cls(config)

    @classmethod
    def discover_and_create(cls, base_path: Union[str, Path] = ".") -> Optional["CompanestOrchestrator"]:
        config = CompanestConfig.discover_config(base_path)
        return cls(config) if config is not None else None

    def get_status(self) -> Dict[str, Any]:
        return {
            "teams_initialized": hasattr(self, "team_registry"),
            **self.get_teams_status(),
        }

    async def get_status_async(self) -> Dict[str, Any]:
        return {
            "teams_initialized": hasattr(self, "team_registry"),
            **await self.get_teams_status_async(),
        }

    def init_teams(
        self,
        base_path: str = ".companest",
        s3_bucket: Optional[str] = None,
        s3_region: str = "us-east-2",
        *, _defer_companies: bool = False,
    ) -> None:
        proxy = self.config.proxy if self.config.proxy.enabled else None
        self.memory = MemoryManager(base_path)

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
        else:
            logger.warning(
                "Unknown memory backend %r; falling back to file backend.",
                self.config.memory_backend,
            )
            self.memory_backend = FileBackend(self.memory)
        self.tool_registry.memory_backend = self.memory_backend

        self.cascade_engine = CascadeEngine()
        self.team_registry = TeamRegistry(
            base_path=f"{base_path}/teams",
            memory=self.memory,
            proxy_config=proxy,
            tool_registry=self.tool_registry,
            cascade_engine=self.cascade_engine,
        )
        self.team_registry.scan_configs()

        skills = load_skills(base_path)
        for skill in skills.values():
            self.tool_registry.register_skill(skill)
        if skills:
            logger.info(f"Loaded {len(skills)} skill(s): {list(skills.keys())}")

        for mcp_cfg in self.config.mcp_servers:
            self.tool_registry.register_external_mcp(mcp_cfg.name, mcp_cfg.to_sdk_config())
        if self.config.mcp_servers:
            logger.info(
                f"Registered {len(self.config.mcp_servers)} external MCP server(s): "
                f"{[s.name for s in self.config.mcp_servers]}"
            )

        _US = _import_user_scheduler()
        self.user_scheduler = _US(data_dir=Path(base_path)) if _US else None

        # Proxy env vars for the raw provider clients (router, Dreamer)
        if self.config.proxy.enabled:
            Pi.configure_proxy(self.config.proxy)

        self.cost_gate = CostGate(
            self.memory, notifier=UserNotifier(),
            event_bus=self.events,
        )
        self.scheduler = Scheduler()

        if s3_bucket:
            self.archiver = MemoryArchiver(
                memory=self.memory,
                bucket=s3_bucket,
                region=s3_region,
            )
            self.scheduler.add(
                "memory_backup",
                self.archiver.backup_snapshot,
                interval=14400,
                run_on_start=False,
            )

        self.dreamer = Dreamer(memory=self.memory, proxy_config=proxy)
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
        if self.user_scheduler is not None:
            self.user_scheduler.set_execution_callback(self.background.execute_scheduled_job)

        self._smart_router = SmartRouter(
            team_registry=self.team_registry,
            memory=self.memory,
            proxy_config=proxy,
            event_bus=self.events,
        )

        for config, always_on in self._pending_teams:
            self.team_registry.register(config, always_on=always_on)
        self._pending_teams.clear()
        for source in self._pending_enrichments:
            self.memory.register_enrichment(source)
        self._pending_enrichments.clear()
        for pattern, team_id, mode in self._pending_bindings:
            self._smart_router.add_binding(pattern, team_id, mode)
        self._pending_bindings.clear()

        self.workspace_registry = WorkspaceRegistry(base_path)
        self.workspace_registry.load()

        self.company_registry = CompanyRegistry(base_path)
        self.company_registry.scan()
        self._ceo_pis: Dict[str, Pi] = {}

        if not _defer_companies:
            for company in self.company_registry.list_enabled():
                try:
                    self._init_company(company)
                except Exception as e:
                    logger.error(f"Failed to init company {company.id}: {e}")
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

    async def company_operation(self, operation, *args, **kwargs):
        """Serialize lifecycle writers; finish an accepted change before cancellation."""
        if self._company_owner is asyncio.current_task():
            return await operation(*args, **kwargs)
        async def invoke():
            self._company_owner = asyncio.current_task()
            try:
                return await operation(*args, **kwargs)
            finally:
                self._company_owner = None
        async with self._company_lock:
            return await finish_on_cancel(invoke())

    async def init_teams_async(self, base_path=".companest", s3_bucket=None, s3_region="us-east-2"):
        """Bootstrap before serving requests without blocking the caller's event loop."""
        async def initialize():
            if hasattr(self, "team_registry"):
                raise OrchestratorError("Teams already initialized")
            await run_sync(self.init_teams, base_path, s3_bucket, s3_region, _defer_companies=True)
            for company in self.company_registry.list_enabled():
                try:
                    await self._init_company_async(company)
                except Exception as exc:
                    logger.error(f"Failed to init company {company.id}: {exc}")
        await self.company_operation(initialize)

    def _get_company_shared_teams(self, company_id: Optional[str]) -> Optional[List[str]]:
        # None = no whitelist (every global team allowed); an unknown company gets [] (none).
        if not company_id:
            return None
        registry = getattr(self, "company_registry", None)
        company = registry.get(company_id) if registry is not None else None
        if company is None:
            return []
        return company.shared_teams

    def can_access_team(self, company_id: Optional[str], team_id: str) -> bool:
        if "/" not in team_id:
            shared_teams = self._get_company_shared_teams(company_id)
            return shared_teams is None or team_id in shared_teams
        return company_id == team_id.split("/", 1)[0]

    def _company_context(self, company, *, worker=False):
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
        if worker:
            ctx.tool_registry = LoopBoundRegistry(self.tool_registry)
            ctx.event_bus = LoopBoundRegistry(self.events)
            ctx.add_binding = bind_to_loop(ctx.add_binding)
        return ctx

    def _register_company_memory_paths(self, company_id, configs):
        root = self.memory.base_path / "companies" / company_id / "teams"
        for tid, cfg in configs.items():
            if cfg.local_id:
                self.memory.register_team_path(tid, root / cfg.local_id)

    def _prepare_company_memory(self, company, available):
        with self.memory._io_lock:
            self._generate_ceo_soul(company, available)
            self._seed_company_memory(company)

    def _seed_company_memory(self, company):
        ns = CompanyMemoryNamespace(self.memory, company.id)
        seed = company.memory_seed
        for key, content in seed.get("shared", {}).items():
            if ns.read_shared(key) is None:
                ns.write_shared(key, content)
        for seed_team_id, team_seed in seed.get("teams", {}).items():
            for key, content in team_seed.items():
                if ns.read_team_memory(seed_team_id, key) is None:
                    ns.write_team_memory(seed_team_id, key, content)

    def _init_company(self, company: CompanyConfig) -> None:
        root = self.memory.base_path / "companies" / company.id / "teams"
        self.team_registry.scan_company_teams(company.id, root)
        self._register_company_memory_paths(company.id, self.team_registry.get_configs_by_company(company.id))
        component = self.company_registry.get_component(company.id)
        if component is not None:
            self._initialized_components.add(company.id)
            try:
                component.on_init(self._company_context(company))
            except Exception as exc:
                logger.error(f"[Company] Component on_init() failed for {company.id}: {exc}")
        self._prepare_company_memory(company, self._smart_router.get_available_teams(company.id))
        self._register_company_runtime(company)

    async def _init_company_async(self, company: CompanyConfig) -> None:
        root = self.memory.base_path / "companies" / company.id / "teams"
        await self.team_registry.scan_company_teams_async(company.id, root)
        configs = self.team_registry.get_configs_by_company(company.id)
        await run_sync(self._register_company_memory_paths, company.id, configs)
        component = self.company_registry.get_component(company.id)
        if component is not None:
            self._initialized_components.add(company.id)
            try:
                await run_component_hook(component.on_init, self._company_context(company, worker=True))
            except Exception as exc:
                logger.error(f"[Company] Component on_init() failed for {company.id}: {exc}")
        available = self._smart_router.get_available_teams(company.id)
        await run_sync(self._prepare_company_memory, company, available)
        self._register_company_runtime(company)

    def _register_company_runtime(self, company):
        team_id = f"company-{company.id}"
        sinks = self._output_sinks.setdefault(company.id, [])
        if not any(isinstance(sink, MemorySink) for sink in sinks):
            sinks.insert(0, MemorySink(self.memory))

        for rb in company.routing_bindings:
            self._smart_router.add_binding(
                rb.get("pattern", ""), rb.get("team_id", ""),
                rb.get("mode", "cascade"),
                owner_company_id=company.id,
            )

        for mcp_cfg in company.mcp_servers:
            cfg = dict(mcp_cfg)
            name = cfg.pop("name", "")
            if name:
                self.tool_registry.register_company_mcp(company.id, name, cfg)

        for st in company.shared_teams or []:
            if st not in self.team_registry.list_teams():
                logger.warning(f"Company {company.id} requests shared team '{st}' which does not exist")

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

        if company.ceo.enabled:
            ceo_config = PiConfig(
                id="ceo",
                model=company.ceo.model,
                tools=["ceo"],
                tools_deny=[],
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

            ceo_pi._extra_tool_context = {
                "run_team_fn": self.run_team,
                "run_auto_fn": self.run_auto,
                "company_id": company.id,
                "company_env": company.env,
                "company_shared_teams": company.shared_teams,
            }

            self._ceo_pis[company.id] = ceo_pi

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
        await self.company_operation(self._apply_company, company_id)

    async def _apply_company(self, company_id: str) -> None:
        config = self.company_registry.get(company_id)
        if self._is_company_initialized(company_id):
            await self.teardown_company(company_id)
        if config and config.enabled:
            await self._init_company_async(config)

    async def teardown_company(self, company_id: str) -> None:
        await self.company_operation(self._teardown_company, company_id)

    async def _teardown_company(self, company_id: str) -> None:
        comp = self.company_registry.get_component(company_id)
        if comp and hasattr(comp, 'on_teardown'):
            try:
                await run_component_hook(comp.on_teardown)
            except Exception as e:
                logger.error(f"[Company] on_teardown failed for {company_id}: {e}")

        self.scheduler.remove_by_scope("company", company_id)
        self._smart_router.remove_bindings_by_owner(company_id)
        await run_sync(self.memory.remove_enrichments_by_company, company_id)
        self.team_registry.unregister_company(company_id)
        await run_sync(self.memory.unregister_company_paths, company_id)
        self.tool_registry.unregister_company_mcp(company_id)
        self._initialized_components.discard(company_id)
        self._ceo_pis.pop(company_id, None)
        self._output_sinks.pop(company_id, None)

        logger.info(f"[Company] Teardown complete: {company_id}")

    def _is_company_initialized(self, company_id: str) -> bool:
        return company_id in self._initialized_components or company_id in self._output_sinks or company_id in self._ceo_pis or bool(
            self.team_registry.get_configs_by_company(company_id)
        )

    def _initialized_company_ids(self) -> List[str]:
        """Return company IDs that may still have runtime resources attached."""
        ids = set(self._ceo_pis) | set(self.company_registry.list_companies())
        ids.update(t.split("/", 1)[0] for t in self.team_registry.list_teams() if "/" in t)
        return sorted(ids)

    async def _run_ceo_cycle(self, company: CompanyConfig, ceo_pi: Pi) -> None:
        cycle_number = self._cycle_numbers.get(company.id, 0) + 1
        self._cycle_numbers[company.id] = cycle_number

        prompt = company.ceo.cycle_prompt or build_cycle_prompt(
            self._smart_router.get_available_teams(company.id), cycle_number,
        )

        result = await ceo_pi.run(
            prompt,
            cascade=True,
            user_context={
                "company_id": company.id,
                "company_context": company.domain,
            },
        )

        cycle_result = {
            "cycle": cycle_number,
            "company_id": company.id,
            "result": result,
            "timestamp": time.time(),
            "summary": result[:500] if result else "",
        }

        for sink in self._output_sinks.get(company.id, []):
            try:
                await sink.emit(company.id, cycle_result)
            except Exception as e:
                logger.error(f"[OutputSink] Failed for {company.id}: {e}")

    def _generate_ceo_soul(self, company: CompanyConfig, available_teams=None) -> None:
        """Generate team and CEO soul.md files for a company (skip if exists)."""
        team_id = f"company-{company.id}"
        base = self.memory.base_path

        team_soul_path = base / "teams" / team_id / "soul.md"
        if not team_soul_path.exists():
            team_soul_path.parent.mkdir(parents=True, exist_ok=True)
            team_soul_path.write_text(
                f"# {company.name} Operations Team\n\n"
                f"This team serves the {company.name} company.\n"
                f"Domain: {company.domain or 'General'}\n",
                encoding="utf-8",
            )

        ceo_soul_path = base / "teams" / team_id / "pis" / "ceo" / "soul.md"
        needs_generation = not ceo_soul_path.exists()
        existing_goals: List[str] = []
        if ceo_soul_path.exists():
            existing_text = ceo_soul_path.read_text(encoding="utf-8")
            # A goals-only placeholder (no operating principles) is regenerated, keeping its goals
            if "## Company Goals" in existing_text and "Operating Principles" not in existing_text:
                needs_generation = True
                for line in existing_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- ") and "## " not in stripped:
                        existing_goals.append(stripped[2:])
        if needs_generation:
            ceo_soul_path.parent.mkdir(parents=True, exist_ok=True)
            merged_goals = list(company.ceo.goals)
            for g in existing_goals:
                if g not in merged_goals:
                    merged_goals.append(g)
            soul_content = generate_ceo_soul(
                company_name=company.name,
                domain=company.domain,
                goals=merged_goals,
                kpis=company.ceo.kpis,
                available_teams=available_teams if available_teams is not None else self._smart_router.get_available_teams(company.id),
            )
            ceo_soul_path.write_text(soul_content, encoding="utf-8")

        mem_dir = base / "teams" / team_id / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

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
        await self.company_operation(self._reload_company_changes)

    async def _reload_company_changes(self) -> None:
        if await self.company_registry.check_for_changes_async():
            logger.info("[Company] Config changes detected, reloading...")
            old_ids = set(self._initialized_company_ids())

            for cid in old_ids:
                try:
                    if self._is_company_initialized(cid):
                        await self.teardown_company(cid)
                except Exception as e:
                    logger.error(f"Failed to teardown company {cid}: {e}")

            await self.company_registry.reload_async()

            new_ids = set()
            for company in self.company_registry.list_enabled():
                new_ids.add(company.id)
                try:
                    await self._init_company_async(company)
                except Exception as e:
                    logger.error(f"Failed to re-init company {company.id}: {e}")

            removed = old_ids - new_ids
            for cid in removed:
                logger.info(f"[Company] Company {cid} removed, already torn down")

            await run_sync(self.memory.clear_cache)

    def set_notification_callback(self, callback) -> None:
        if not hasattr(self, "background"):
            return
        self.background.set_notification_callback(callback)

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
        if not hasattr(self, "team_registry"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

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

        if company_id and not company_budget_hourly:
            company = self.company_registry.get(company_id)
            if company:
                company_budget_hourly = company.preferences.budget_hourly_usd

        # Company isolation: private teams are only reachable by their owner
        if not self.can_access_team(company_id, team_id):
            raise OrchestratorError(
                f"Access denied: company '{company_id}' cannot access team '{team_id}'"
            )

        try:
            team = self.team_registry.get_or_create(team_id)
        except TeamError as e:
            raise OrchestratorError(f"Team routing failed: {e}")
        effective_mode = mode if mode is not None else team.mode

        if not skip_cost_check:
            lead_config = team.get_lead_config()
            model = lead_config.model if lead_config else "deepseek-chat"

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

        extra_context: Dict[str, Any] = {}
        workspace_id = user_context.get("workspace_id")
        ws = self.workspace_registry.get(workspace_id) if workspace_id else None
        if ws:
            extra_context["workspace_path"] = ws.path
            extra_context["workspace_context"] = await run_sync(self.workspace_registry.build_context, workspace_id)
        extra_context["company_shared_teams"] = self._get_company_shared_teams(company_id)
        for pi in team.pis.values():
            pi.user_scheduler = self.user_scheduler

        event_data = {"team_id": team_id, "mode": effective_mode, "task_preview": task[:200]}
        if company_id:
            event_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_STARTED, event_data)

        # acquire/release keeps the team from being evicted mid-task
        self.team_registry.acquire(team_id)
        tool_context_token = _run_tool_context.set(extra_context)
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
            _run_tool_context.reset(tool_context_token)
            self.team_registry.release(team_id)

        completed_data = {"team_id": team_id, "mode": effective_mode}
        if company_id:
            completed_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_COMPLETED, completed_data)

        # Recorded spend is the estimate; actual token counts are not known here
        if not skip_cost_check:
            try:
                lead_config = team.get_lead_config()
                model = lead_config.model if lead_config else "deepseek-chat"
                estimate = self.cost_gate.estimate_cost(task, model, team_id)
                await run_sync(self.cost_gate.record_spending,
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
        if not hasattr(self, "_smart_router"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

        user_context = dict(user_context) if user_context else {}
        preferred_teams = None
        if "company_id" not in user_context:
            resolve_result = self.company_registry.resolve(
                user_context.get("channel"),
                user_context.get("chat_id"),
                user_context.get("user_id"),
            )
            direct = None
            if resolve_result.company:
                user_context["company_id"] = resolve_result.company.id
                if not user_context.get("company_context"):
                    user_context["company_context"] = resolve_result.company.domain
                if resolve_result.company.preferences.preferred_teams:
                    preferred_teams = resolve_result.company.preferences.preferred_teams
            elif resolve_result.global_binding:
                gb = resolve_result.global_binding
                direct = (gb.team_id, gb.mode, "Global binding match")
            elif user_context.get("binding_team_id"):
                # Binding hint resolved by the gateway (companest/gateway/channel.py)
                direct = (
                    user_context["binding_team_id"],
                    user_context.get("binding_mode", "default"),
                    "Gateway binding hint (legacy)",
                )
            if direct:
                direct_team, direct_mode, reasoning = direct
                effective_mode = mode if mode is not None else direct_mode
                result_str = await self.run_team(
                    task, direct_team, mode=effective_mode,
                    on_progress=on_progress, user_context=user_context,
                )
                decision = RoutingDecision(
                    teams=[TeamAssignment(team_id=direct_team, instruction=task)],
                    strategy="single",
                    reasoning=reasoning,
                    confidence=1.0,
                    mode=effective_mode,
                )
                return result_str, decision

        company_id = user_context.get("company_id")
        if preferred_teams is None and company_id:
            company = self.company_registry.get(company_id)
            if company and company.preferences.preferred_teams:
                preferred_teams = company.preferences.preferred_teams

        decision = await self._smart_router.route(
            task, preferred_teams=preferred_teams,
            company_id=company_id,
            shared_teams=self._get_company_shared_teams(company_id),
        )

        if decision.declined:
            raise OrchestratorError(
                f"No suitable team found: {decision.decline_reason or 'task does not match any team'}",
                details={"declined": True, "reasoning": decision.reasoning},
            )

        effective_mode = mode if mode is not None else decision.mode

        if on_progress:
            teams_str = "+".join(t.team_id for t in decision.teams)
            await on_progress(
                f"\U0001f50d \u2192 {teams_str} ({effective_mode}, {decision.confidence:.0%})"
            )

        routed_ids = {t.team_id for t in decision.teams}
        if self._info_refresh_team not in routed_ids:
            if on_progress:
                await on_progress("\U0001f4e1 Refreshing info feeds...")
            await self.background.refresh_info_for_task(task)

        if len(decision.teams) == 1:
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

        result = await run_multi_team(
            self.run_team, decision, mode=effective_mode,
            on_progress=on_progress, user_context=user_context,
        )
        return result, decision

    def get_teams_status(self) -> Dict[str, Any]:
        status = self._get_runtime_status()
        if hasattr(self, "cost_gate"):
            status["finance"] = self.cost_gate.get_spending_summary()
        return status

    async def get_teams_status_async(self) -> Dict[str, Any]:
        status = self._get_runtime_status()
        if hasattr(self, "cost_gate"):
            status["finance"] = await run_sync(self.cost_gate.get_spending_summary)
        return status

    def _get_runtime_status(self) -> Dict[str, Any]:
        status = {}
        if hasattr(self, "team_registry"):
            status["teams"] = self.team_registry.get_fleet_status()
        if hasattr(self, "scheduler"):
            status["scheduler"] = self.scheduler.get_status()
        return status
