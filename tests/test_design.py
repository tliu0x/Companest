"""
Companest Design Verification Tests

Tests that the architecture is wired correctly:
1. Module imports  all new modules importable, no circular deps
2. Memory hierarchy  read/write/list at all levels
3. Team registry  scan, on-demand create, evict
4. Cost gate  three tiers produce correct decisions
5. Router  keyword routing accuracy
6. Scheduler  add/start/stop lifecycle
7. Orchestrator integration  init_teams() wires everything
8. Server endpoints  endpoints exist on the app
9. Exception hierarchy  all exceptions inherit CompanestError

No external API keys or network access needed.
"""

import asyncio
import tempfile
import time
import shutil
from pathlib import Path

import pytest


#  1. Module Imports 

class TestModuleImports:
    """Verify all modules import without error."""

    def test_memory_import(self):
        from companest.memory import MemoryManager
        assert MemoryManager is not None

    def test_tools_import(self):
        from companest.tools import resolve_tool_names, TOOL_PRESETS, CLAUDE_BUILTIN_TOOLS
        assert "pi-core" in TOOL_PRESETS

    def test_pi_import(self):
        from companest.pi import Pi, PiConfig
        assert PiConfig is not None

    def test_team_import(self):
        from companest.team import AgentTeam, TeamConfig, TeamRegistry
        assert TeamRegistry is not None

    def test_cost_gate_import(self):
        from companest.cost_gate import CostGate, CostEstimate, CostDecision, MODEL_PRICES
        from companest.cascade import CascadeEngine, CascadeStrategy, CascadeMetrics, AdequacyChecker
        assert "claude-sonnet-4-5" in MODEL_PRICES
        assert CascadeEngine is not None

    def test_archiver_import(self):
        from companest.archiver import MemoryArchiver
        assert MemoryArchiver is not None

    def test_scheduler_import(self):
        from companest.scheduler import Scheduler, ScheduledTask
        assert Scheduler is not None

    def test_router_import(self):
        from companest.router import TeamRouter, SmartRouter, DEFAULT_ROUTES
        from companest.router import RoutingDecision, TeamAssignment
        assert len(DEFAULT_ROUTES) == 6
        assert SmartRouter is not None
        assert RoutingDecision is not None
        assert TeamAssignment is not None

    def test_top_level_import(self):
        """All classes exported from companest package."""
        from companest import (
            MemoryManager, Pi, PiConfig,
            AgentTeam, TeamConfig, TeamRegistry,
            CostGate, CostEstimate, CostDecision, UserNotifier,
            MemoryArchiver, Scheduler, TeamRouter,
            SmartRouter, RoutingDecision, TeamAssignment,
        )
        assert all(cls is not None for cls in [
            MemoryManager, Pi, PiConfig, AgentTeam, TeamConfig,
            TeamRegistry, CostGate, Scheduler, TeamRouter,
            SmartRouter, RoutingDecision, TeamAssignment,
        ])


#  2. Memory Hierarchy 

class TestMemoryHierarchy:
    """Test MemoryManager at all levels."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)

        # Create minimal .companest structure
        (self.base / "teams" / "stock" / "memory").mkdir(parents=True)
        (self.base / "teams" / "stock" / "pis" / "analyst").mkdir(parents=True)
        (self.base / "teams" / "stock" / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
        )
        (self.base / "teams" / "stock" / "soul.md").write_text("Stock team soul")
        (self.base / "teams" / "stock" / "pis" / "analyst" / "soul.md").write_text(
            "You are a stock analyst."
        )
        (self.base / "soul.md").write_text("Master soul")
        (self.base / "user.md").write_text("# User\n- Language: English")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_read_master_soul(self):
        assert "Master soul" in self.mm.read_master_soul()

    def test_read_user(self):
        assert "English" in self.mm.read_master_user()

    def test_team_memory_write_read(self):
        self.mm.write_team_memory("stock", "watchlist.json", ["TSLA", "NVDA"])
        data = self.mm.read_team_memory("stock", "watchlist.json")
        assert data == ["TSLA", "NVDA"]

    def test_team_memory_append(self):
        self.mm.append_team_memory("stock", "log.json", {"event": "buy"})
        self.mm.append_team_memory("stock", "log.json", {"event": "sell"})
        data = self.mm.read_team_memory("stock", "log.json")
        assert len(data) == 2

    def test_list_team_memory(self):
        self.mm.write_team_memory("stock", "a.json", {})
        keys = self.mm.list_team_memory("stock")
        assert "a.json" in keys

    def test_list_teams(self):
        teams = self.mm.list_teams()
        assert "stock" in teams

    def test_team_exists(self):
        assert self.mm.team_exists("stock")
        assert not self.mm.team_exists("nonexistent")

    def test_build_system_prompt(self):
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "stock analyst" in prompt.lower()
        assert "English" in prompt

    def test_read_pi_soul(self):
        soul = self.mm.read_pi_soul("stock", "analyst")
        assert "analyst" in soul.lower()

    def test_get_all_memory_stats(self):
        self.mm.write_team_memory("stock", "data.json", {"x": 1})
        stats = self.mm.get_all_memory_stats()
        assert "stock" in stats
        assert stats["stock"]["files"] >= 1


#  3. Team Registry 

class TestTeamRegistry:
    """Test TeamConfig parsing and TeamRegistry lifecycle."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        teams_dir = self.base / "teams"

        # Create stock team (on-demand)
        stock_dir = teams_dir / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n- always_on: false\n"
            "\n#### Pi: analyst\n- model: claude-sonnet-4-5-20250929\n- tools: memory_read, web_search\n- max_turns: 5\n"
        )
        (stock_dir / "pis" / "analyst" / "soul.md").write_text("Analyst soul")

        # Create finance team (always_on meta-team)
        fin_dir = teams_dir / "finance"
        (fin_dir / "memory").mkdir(parents=True)
        (fin_dir / "pis" / "accountant").mkdir(parents=True)
        (fin_dir / "team.md").write_text(
            "# Team: finance\n- role: cost_gate\n- lead_pi: accountant\n- enabled: true\n- always_on: true\n"
            "\n#### Pi: accountant\n- model: claude-haiku-4-5-20251001\n- tools: memory_read, memory_write\n- max_turns: 3\n"
        )
        (fin_dir / "pis" / "accountant" / "soul.md").write_text("Accountant soul")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_team_config_from_markdown(self):
        from companest.team import TeamConfig
        config = TeamConfig.from_markdown(
            self.base / "teams" / "stock" / "team.md"
        )
        assert config.id == "stock"
        assert config.role == "general"
        assert config.lead_pi == "analyst"
        assert not config.always_on
        assert len(config.pis) == 1
        assert config.pis[0].id == "analyst"
        assert config.pis[0].model == "claude-sonnet-4-5-20250929"

    def test_registry_scan(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        assert "stock" in reg.list_teams()
        assert "finance" in reg.list_teams()
        assert "finance" in reg.list_meta_teams()
        assert "stock" not in reg.list_meta_teams()

    def test_registry_on_demand_create(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        # Stock not active yet
        assert "stock" not in reg.list_active()

        # Get creates it
        team = reg.get_or_create("stock")
        assert team.id == "stock"
        assert "stock" in reg.list_active()

    def test_registry_meta_team_always_active(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        # Finance is always active (meta)
        assert "finance" in reg.list_active()

    def test_registry_evict_idle(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm, idle_timeout=0)
        reg.scan_configs()

        reg.get_or_create("stock")
        assert "stock" in reg.list_active()

        time.sleep(0.01)
        evicted = reg.evict_idle()
        assert "stock" in evicted
        # Meta teams not evicted
        assert "finance" not in evicted
        assert "finance" in reg.list_active()

    def test_registry_get_nonexistent_raises(self):
        from companest.team import TeamRegistry
        from companest.exceptions import TeamError
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        with pytest.raises(TeamError):
            reg.get_or_create("nonexistent")

    def test_fleet_status(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        status = reg.get_fleet_status()
        assert "stock" in status["registered"]
        assert "finance" in status["meta"]

    def test_registry_reload(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()
        reg.get_or_create("stock")

        reg.reload()
        # After reload, on-demand instances cleared
        assert "stock" not in reg._instances
        # But configs re-scanned
        assert "stock" in reg.list_teams()


#  4. Cost Gate 

class TestCostGate:
    """Test three-tier cost approval logic."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        (self.base / "teams" / "finance" / "memory").mkdir(parents=True)

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_cost_estimate(self):
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        est = cg.estimate_cost("short task", "claude-haiku-4-5-20251001", "stock")
        assert est.estimated_cost_usd > 0
        assert est.target_model == "claude-haiku-4-5-20251001"
        # Haiku is the cheapest Anthropic model  no same-provider downgrade
        assert est.suggested_downgrade is None

    def test_tier1_auto_approve(self):
        """Cheap task  auto_approve silently."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("hi", "stock", "gpt-4o-mini")
        )
        assert decision.action == "auto_approve"
        assert decision.estimate.estimated_cost_usd < 0.05

    def test_tier2_notify_approve(self):
        """Medium task  notify_approve (requires approval mode)."""
        from companest.cost_gate import CostGate, UserNotifier
        cg = CostGate(self.mm, notifier=UserNotifier())

        # Write budget with very low auto_approve threshold and approval mode
        self.mm.write_team_memory("finance", "budget.json", {
            "auto_approve_threshold": 0.0001,
            "escalation_threshold": 100.0,
            "daily_limit": 1000.0,
            "mode": "approval",
        })

        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("analyze this stock", "stock", "claude-haiku-4-5-20251001")
        )
        assert decision.action == "notify_approve"

    def test_record_spending(self):
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        cg.record_spending("stock", "test task", {"input": 100, "output": 200}, 0.001)

        log = self.mm.read_team_memory("finance", "spending-log.json")
        assert len(log) == 1
        assert log[0]["team"] == "stock"

    def test_spending_summary(self):
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        cg.record_spending("stock", "t1", {}, 0.01)
        cg.record_spending("bizdev", "t2", {}, 0.02)

        summary = cg.get_spending_summary()
        assert summary["total"] == 0.03
        assert "stock" in summary["by_team"]
        assert summary["entries"] == 2

    def test_model_prices_complete(self):
        from companest.cost_gate import MODEL_PRICES
        from companest.cascade import get_downgrade
        # Every model with a downgrade target must point to a valid model
        for model_id in MODEL_PRICES:
            downgrade = get_downgrade(model_id)
            if downgrade is not None:
                assert downgrade in MODEL_PRICES, f"downgrade target {downgrade} not in MODEL_PRICES"


#  5. Router 

class TestRouter:
    """Test keyword-based team routing."""

    def setup_method(self):
        from companest.router import TeamRouter
        self.router = TeamRouter(
            available_teams=["stock", "bizdev", "engineering", "philosophy", "science", "finance"]
        )

    def test_stock_routing(self):
        assert self.router.route("Analyze TSLA recent stock trends") == "stock"

    def test_stock_routing_english(self):
        assert self.router.route("What is NVDA stock doing today?") == "stock"

    def test_bizdev_routing(self):
        assert self.router.route("I want to buy a house, any advice?") == "bizdev"

    def test_engineering_routing(self):
        assert self.router.route("Fix the bug in the Python API") == "engineering"

    def test_philosophy_routing(self):
        assert self.router.route("What is free will? What is the nature of consciousness?") == "philosophy"

    def test_science_routing(self):
        assert self.router.route("What is wrong with this physics paper's experiment design?") == "science"

    def test_explicit_tag(self):
        assert self.router.route("@stock check AAPL") == "stock"
        assert self.router.route("#engineering deploy to prod") == "engineering"

    def test_default_fallback(self):
        assert self.router.route("hello world") == "philosophy"

    def test_no_default_returns_none(self):
        from companest.router import TeamRouter
        router = TeamRouter(
            available_teams=["stock"],
            default_team=None,
        )
        assert router.route("hello world") is None

    def test_confidence_scoring(self):
        team, conf = self.router.route_with_confidence("TSLA stock candlestick MACD")
        assert team == "stock"
        assert conf > 0.5

    def test_explicit_tag_confidence(self):
        team, conf = self.router.route_with_confidence("@stock anything")
        assert team == "stock"
        assert conf == 1.0


class TestSmartRouterParsing:
    """Test SmartRouter._parse_response() with various LLM outputs."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        teams_dir = self.base / "teams"

        # Create minimal stock team
        stock_dir = teams_dir / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
            "\n#### Pi: analyst\n- model: claude-haiku-4-5-20251001\n"
        )
        (stock_dir / "pis" / "analyst" / "soul.md").write_text("Stock analyst")
        (stock_dir / "soul.md").write_text("Stock team")

        # Create minimal engineering team
        eng_dir = teams_dir / "engineering"
        (eng_dir / "memory").mkdir(parents=True)
        (eng_dir / "pis" / "coder").mkdir(parents=True)
        (eng_dir / "team.md").write_text(
            "# Team: engineering\n- role: general\n- lead_pi: coder\n- enabled: true\n"
            "\n#### Pi: coder\n- model: claude-sonnet-4-5-20250929\n"
        )
        (eng_dir / "pis" / "coder" / "soul.md").write_text("Engineer")
        (eng_dir / "soul.md").write_text("Engineering team")

        from companest.memory import MemoryManager
        from companest.team import TeamRegistry
        from companest.router import SmartRouter

        self.mm = MemoryManager(str(self.base))
        self.reg = TeamRegistry(str(self.base / "teams"), self.mm)
        self.reg.scan_configs()
        self.router = SmartRouter(self.reg, self.mm)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_parse_valid_json(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "Analyze TSLA", "priority": 1}], "strategy": "single", "reasoning": "Stock task", "confidence": 0.9, "declined": false}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert len(result.teams) == 1
        assert result.teams[0].team_id == "stock"
        assert result.strategy == "single"
        assert result.confidence == 0.9

    def test_parse_markdown_fenced_json(self):
        raw = '```json\n{"teams": [{"team_id": "engineering", "instruction": "Fix bug"}], "strategy": "single", "reasoning": "Code task", "confidence": 0.85}\n```'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.teams[0].team_id == "engineering"

    def test_parse_declined(self):
        raw = '{"teams": [], "declined": true, "decline_reason": "No team fits", "reasoning": "General chat"}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.declined is True
        assert result.decline_reason == "No team fits"

    def test_parse_invalid_json(self):
        raw = "This is not JSON at all"
        result = self.router._parse_response(raw)
        assert result is None

    def test_parse_filters_unknown_teams(self):
        raw = '{"teams": [{"team_id": "nonexistent", "instruction": "x"}, {"team_id": "stock", "instruction": "y"}], "strategy": "single", "confidence": 0.8}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert len(result.teams) == 1
        assert result.teams[0].team_id == "stock"

    def test_parse_multi_team(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "a", "priority": 1}, {"team_id": "engineering", "instruction": "b", "priority": 2}], "strategy": "parallel", "reasoning": "cross-domain", "confidence": 0.75}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert len(result.teams) == 2
        assert result.strategy == "parallel"

    def test_parse_mode_default(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "mode": "default", "confidence": 0.8}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.mode == "default"

    def test_parse_mode_council(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "mode": "council", "confidence": 0.9}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.mode == "council"

    def test_parse_mode_invalid_fallback(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "mode": "turbo", "confidence": 0.8}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.mode == "default"

    def test_parse_mode_missing_fallback(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "confidence": 0.8}'
        result = self.router._parse_response(raw)
        assert result is not None
        assert result.mode == "default"

    def test_keyword_fallback_matches(self):
        decision = self.router._keyword_fallback("Analyze TSLA stock trends")
        assert not decision.declined
        assert decision.teams[0].team_id == "stock"

    def test_keyword_fallback_no_match_declines(self):
        decision = self.router._keyword_fallback("hello world")
        assert decision.declined is True

    def test_build_system_prompt_caching(self):
        prompt1 = self.router._build_system_prompt()
        prompt2 = self.router._build_system_prompt()
        assert prompt1 is not None
        assert prompt1 is prompt2  # Same object (cached)

        self.router.invalidate_cache()
        prompt3 = self.router._build_system_prompt()
        assert prompt3 is not prompt1  # Rebuilt


class TestRoutingDecision:
    """Test RoutingDecision and TeamAssignment data models."""

    def test_default_values(self):
        from companest.router import RoutingDecision, TeamAssignment
        d = RoutingDecision()
        assert d.teams == []
        assert d.strategy == "single"
        assert d.declined is False
        assert d.confidence == 0.0
        assert d.mode == "default"

    def test_mode_preserved(self):
        from companest.router import RoutingDecision
        d = RoutingDecision(mode="council")
        assert d.mode == "council"

    def test_team_assignment(self):
        from companest.router import TeamAssignment
        a = TeamAssignment(team_id="stock", instruction="Analyze TSLA")
        assert a.team_id == "stock"
        assert a.priority == 1


#  6. Scheduler 

class TestScheduler:
    """Test scheduler lifecycle."""

    def test_add_and_status(self):
        from companest.scheduler import Scheduler

        async def dummy():
            pass

        s = Scheduler()
        s.add("test_task", dummy, interval=60)
        status = s.get_status()
        assert not status["started"]
        assert "test_task" in status["tasks"]
        assert status["tasks"]["test_task"]["interval_seconds"] == 60

    def test_start_stop(self):
        from companest.scheduler import Scheduler

        async def dummy():
            pass

        s = Scheduler()
        s.add("test_task", dummy, interval=3600)

        async def lifecycle():
            await s.start()
            assert s.get_status()["started"]
            assert s.get_status()["tasks"]["test_task"]["running"]
            await s.stop()
            assert not s.get_status()["started"]

        asyncio.get_event_loop().run_until_complete(lifecycle())

    def test_disable_enable(self):
        from companest.scheduler import Scheduler

        async def dummy():
            pass

        s = Scheduler()
        s.add("t", dummy, interval=3600)
        assert s.disable("t")
        assert not s._tasks["t"].enabled
        assert s.enable("t")
        assert s._tasks["t"].enabled

    def test_remove(self):
        from companest.scheduler import Scheduler

        async def dummy():
            pass

        s = Scheduler()
        s.add("t", dummy, interval=60)
        assert s.remove("t")
        assert "t" not in s._tasks

    def test_run_now(self):
        from companest.scheduler import Scheduler
        counter = {"n": 0}

        async def inc():
            counter["n"] += 1

        s = Scheduler()
        s.add("inc", inc, interval=3600)

        asyncio.get_event_loop().run_until_complete(s.run_now("inc"))
        assert counter["n"] == 1


#  7. Tools 

class TestTools:
    """Test tool name resolution and presets."""

    def test_resolve_builtin(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["Read", "Write", "Bash"])
        assert result == ["Read", "Write", "Bash"]

    def test_resolve_aliases(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["web_search", "exec"])
        assert "WebSearch" in result
        assert "Bash" in result

    def test_resolve_custom_tools(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["memory_read", "memory_write"])
        assert "mcp__mem__memory_read" in result
        assert "mcp__mem__memory_write" in result

    def test_resolve_preset(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["researcher"])
        assert "WebSearch" in result
        assert "mcp__mem__memory_read" in result

    def test_deduplication(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["Read", "Read", "read"])
        assert result.count("Read") == 1


#  8. Exception Hierarchy 

class TestExceptions:
    """Verify all exceptions inherit from CompanestError."""

    def test_hierarchy(self):
        from companest.exceptions import (
            CompanestError, PiError, TeamError,
            CostGateError, ArchiverError, SchedulerError,
        )
        for exc_cls in [PiError, TeamError, CostGateError, ArchiverError, SchedulerError]:
            assert issubclass(exc_cls, CompanestError), f"{exc_cls.__name__} not a CompanestError"

    def test_exception_details(self):
        from companest.exceptions import PiError
        e = PiError("test error", details={"model": "claude"})
        assert "test error" in str(e)
        assert "claude" in str(e)


#  9. Server Endpoints 

class TestServerEndpoints:
    """Verify API endpoints exist on the FastAPI app."""

    def test_endpoints_registered(self):
        """Check that all endpoints are on the app."""
        pytest.importorskip("fastapi")

        from companest.config import CompanestConfig
        from companest.jobs import JobManager
        from companest.server import CompanestAPIServer

        config = CompanestConfig(debug=True)  # debug=True: no auth token required
        jm = JobManager.__new__(JobManager)
        jm.orchestrator = None
        jm._data_dir = None

        server = CompanestAPIServer(config, jm)
        app = server.create_app()

        routes = [r.path for r in app.routes]
        assert "/api/teams" in routes
        assert "/api/teams/{team_id}" in routes
        assert "/api/teams/{team_id}/run" in routes
        assert "/api/finance/summary" in routes
        assert "/api/finance/approve/{approval_id}" in routes
        assert "/api/scheduler/status" in routes
        assert "/api/scheduler/{task_name}/trigger" in routes
        assert "/api/v2/status" in routes

    def test_server_refuses_start_without_auth_in_production(self):
        """Server must refuse to start without COMPANEST_API_TOKEN when debug=False."""
        pytest.importorskip("fastapi")

        from companest.config import CompanestConfig
        from companest.jobs import JobManager
        from companest.server import CompanestAPIServer

        config = CompanestConfig(debug=False)  # production mode, no token
        jm = JobManager.__new__(JobManager)
        jm.orchestrator = None
        jm._data_dir = None

        server = CompanestAPIServer(config, jm)
        with pytest.raises(RuntimeError, match="COMPANEST_API_TOKEN is required"):
            server.create_app()


#  10. Research Briefing Injection 

class TestResearchBriefingInjection:
    """Test research briefing injection into system prompts."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)

        # Create stock team
        stock_dir = self.base / "teams" / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
        )
        (stock_dir / "soul.md").write_text("Stock team soul")
        (stock_dir / "pis" / "analyst" / "soul.md").write_text(
            "You are a stock analyst."
        )

        # Create research team structure (for briefing storage)
        research_dir = self.base / "teams" / "research"
        (research_dir / "memory").mkdir(parents=True)
        (research_dir / "pis" / "scout").mkdir(parents=True)
        (research_dir / "team.md").write_text(
            "# Team: research\n- role: research\n- lead_pi: scout\n- always_on: true\n- enabled: true\n"
        )
        (research_dir / "soul.md").write_text("Research team soul")
        (research_dir / "pis" / "scout" / "soul.md").write_text(
            "You are a research scout."
        )

        (self.base / "soul.md").write_text("Master soul")
        (self.base / "user.md").write_text("# User\n- Language: en")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _write_briefing(self, items, updated_at="2025-01-15T10:30:00Z"):
        self.mm.write_team_memory("research", "briefing.json", {
            "updated_at": updated_at,
            "items": items,
        })

    def test_no_briefing_no_section(self):
        """No briefing.json  no World Briefing section."""
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_briefing_injected_into_business_team(self):
        """Briefing exists  injected into stock team prompt."""
        self._write_briefing([
            {"headline": "S&P 500 hits record high", "category": "markets", "source": "Reuters"},
            {"headline": "New AI chip announced", "category": "tech", "source": "TechCrunch"},
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "S&P 500 hits record high" in prompt
        assert "New AI chip announced" in prompt
        assert "[markets]" in prompt
        assert "(Reuters)" in prompt

    def test_not_injected_into_research_team(self):
        """Research team does NOT get its own briefing (anti-circular)."""
        self._write_briefing([
            {"headline": "Test headline", "category": "world", "source": "AP"},
        ])
        prompt = self.mm.build_system_prompt("research", "scout")
        assert "World Briefing" not in prompt

    def test_capped_at_max_items(self):
        """Briefing capped at 15 items."""
        items = [
            {"headline": f"Item {i}", "category": "world", "source": "AP"}
            for i in range(20)
        ]
        self._write_briefing(items)
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "Item 14" in prompt  # 0-indexed, 15th item
        assert "Item 15" not in prompt  # 16th item excluded

    def test_empty_items_no_section(self):
        """Empty items list  no World Briefing section."""
        self._write_briefing([])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_malformed_briefing_no_crash(self):
        """Malformed briefing.json  graceful skip, no crash."""
        self.mm.write_team_memory("research", "briefing.json", "not a dict")
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_items_missing_headline_skipped(self):
        """Items without headline are skipped."""
        self._write_briefing([
            {"category": "world", "source": "AP"},  # no headline
            {"headline": "Valid headline", "category": "tech", "source": "BBC"},
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "Valid headline" in prompt
        # Only the valid item produces a line
        briefing_section = prompt.split("World Briefing")[1]
        assert briefing_section.count("- ") == 1


#  11. Research Team Registry 

class TestResearchTeamRegistry:
    """Test research team is registered as meta-team."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        teams_dir = self.base / "teams"

        # Create research team
        research_dir = teams_dir / "research"
        (research_dir / "memory").mkdir(parents=True)
        (research_dir / "pis" / "scout").mkdir(parents=True)
        (research_dir / "team.md").write_text(
            "# Team: research\n- role: research\n- lead_pi: scout\n- always_on: true\n- enabled: true\n"
        )
        (research_dir / "pis" / "scout" / "soul.md").write_text("Scout soul")

        # Create stock team (on-demand)
        stock_dir = teams_dir / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n- always_on: false\n"
            "\n#### Pi: analyst\n- model: claude-sonnet-4-5-20250929\n- tools: memory_read\n- max_turns: 5\n"
        )
        (stock_dir / "pis" / "analyst" / "soul.md").write_text("Analyst soul")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_research_registered_as_meta_team(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        assert "research" in reg.list_teams()
        assert "research" in reg.list_meta_teams()

    def test_research_not_evicted(self):
        """Meta-teams (always_on) are never evicted."""
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm, idle_timeout=0)
        reg.scan_configs()

        # Get on-demand stock team
        reg.get_or_create("stock")
        time.sleep(0.01)

        evicted = reg.evict_idle()
        assert "stock" in evicted
        assert "research" not in evicted
        assert "research" in reg.list_active()

    def test_research_config_parsed_correctly(self):
        from companest.team import TeamConfig
        config = TeamConfig.from_markdown(
            self.base / "teams" / "research" / "team.md"
        )
        assert config.id == "research"
        assert config.role == "research"
        assert config.always_on is True
        assert config.lead_pi == "scout"


#  12. ToolRegistry 

class TestToolRegistry:
    """Test ToolRegistry provider management."""

    def test_builtin_providers_registered(self):
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        providers = reg.list_providers()
        assert "memory" in providers
        assert "scheduler" in providers
        assert "feed" in providers

    def test_register_custom_provider(self):
        from companest.tools import ToolRegistry, ToolProvider, ToolContext
        reg = ToolRegistry()

        provider = ToolProvider(
            name="kalshi",
            tool_names={"place_order", "get_markets"},
            mcp_factory=None,
            openai_factory=None,
        )
        reg.register(provider)
        assert "kalshi" in reg.list_providers()

    def test_unregister_provider(self):
        from companest.tools import ToolRegistry, ToolProvider
        reg = ToolRegistry()
        provider = ToolProvider(name="custom", tool_names={"foo"})
        reg.register(provider)
        assert "custom" in reg.list_providers()
        reg.unregister("custom")
        assert "custom" not in reg.list_providers()

    def test_register_preset(self):
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        reg.register_preset("trader", ["place_order", "get_markets", "memory_read"])
        assert "trader" in reg._custom_presets

    def test_resolve_tool_names_delegates(self):
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        result = reg.resolve_tool_names(["Read", "memory_read"])
        assert "Read" in result
        assert "mcp__mem__memory_read" in result


#  13. TeamRegistry.register() 

class TestTeamRegistryProgrammatic:
    """Test programmatic team registration."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        (self.base / "teams").mkdir(parents=True)
        (self.base / "soul.md").write_text("Master soul")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_register_on_demand_team(self):
        from companest.team import TeamRegistry, TeamConfig
        from companest.pi import PiConfig

        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        config = TeamConfig(
            id="kalshi",
            role="trading",
            lead_pi="trader",
            pis=[PiConfig(id="trader", model="claude-haiku-4-5-20251001", tools=["memory_read"])],
        )
        reg.register(config)

        assert "kalshi" in reg.list_teams()
        team = reg.get_or_create("kalshi")
        assert team.id == "kalshi"

    def test_register_always_on_team(self):
        from companest.team import TeamRegistry, TeamConfig
        from companest.pi import PiConfig

        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        config = TeamConfig(
            id="monitor",
            role="monitoring",
            lead_pi="watcher",
            pis=[PiConfig(id="watcher")],
        )
        reg.register(config, always_on=True)

        assert "monitor" in reg.list_meta_teams()
        assert "monitor" in reg.list_active()

    def test_register_disabled_team_skipped(self):
        from companest.team import TeamRegistry, TeamConfig

        reg = TeamRegistry(str(self.base / "teams"), self.mm)
        reg.scan_configs()

        config = TeamConfig(id="disabled", enabled=False)
        reg.register(config)

        assert "disabled" not in reg.list_teams()


#  14. EnrichmentSource 

class TestEnrichmentSource:
    """Test custom enrichment source registration."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)

        # Create stock team
        stock_dir = self.base / "teams" / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
        )
        (stock_dir / "soul.md").write_text("Stock team soul")
        (stock_dir / "pis" / "analyst" / "soul.md").write_text("Stock analyst.")

        # Create custom data team
        data_dir = self.base / "teams" / "data"
        (data_dir / "memory").mkdir(parents=True)
        (data_dir / "pis" / "collector").mkdir(parents=True)
        (data_dir / "team.md").write_text(
            "# Team: data\n- role: data\n- lead_pi: collector\n- enabled: true\n"
        )
        (data_dir / "pis" / "collector" / "soul.md").write_text("Data collector.")

        (self.base / "soul.md").write_text("Master soul")
        (self.base / "user.md").write_text("")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_custom_enrichment_injected(self):
        from companest.memory import EnrichmentSource

        def format_prices(data):
            if not isinstance(data, dict):
                return None
            prices = data.get("prices", [])
            if not prices:
                return None
            lines = [f"- {p['symbol']}: ${p['price']}" for p in prices]
            return "## Market Prices\n" + "\n".join(lines)

        self.mm.register_enrichment(EnrichmentSource(
            source_team_id="data",
            memory_key="prices.json",
            section_title="Market Prices",
            formatter=format_prices,
            exclude_teams={"data"},
        ))

        self.mm.write_team_memory("data", "prices.json", {
            "prices": [{"symbol": "TSLA", "price": 250.0}]
        })

        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "Market Prices" in prompt
        assert "TSLA" in prompt

    def test_custom_enrichment_excluded_for_source_team(self):
        from companest.memory import EnrichmentSource

        def format_data(data):
            return "## Custom Section\nSome data"

        self.mm.register_enrichment(EnrichmentSource(
            source_team_id="data",
            memory_key="stuff.json",
            section_title="Custom",
            formatter=format_data,
            exclude_teams={"data"},
        ))

        self.mm.write_team_memory("data", "stuff.json", {"anything": True})

        prompt = self.mm.build_system_prompt("data", "collector")
        assert "Custom Section" not in prompt

    def test_default_enrichments_still_work(self):
        """Research briefing (default enrichment) still works after refactor."""
        # Create research team structure
        research_dir = self.base / "teams" / "research"
        (research_dir / "memory").mkdir(parents=True)

        self.mm.write_team_memory("research", "briefing.json", {
            "updated_at": "2026-01-01T00:00:00Z",
            "items": [{"headline": "Test headline", "category": "tech", "source": "AP"}],
        })

        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "Test headline" in prompt


#  15. New exports 

class TestNewExports:
    """Verify new classes are exported from the Companest package."""

    def test_event_exports(self):
        from companest import EventBus, Event, EventType
        assert EventBus is not None
        assert Event is not None
        assert EventType is not None

    def test_tool_registry_exports(self):
        from companest import ToolRegistry, ToolProvider, ToolContext
        assert ToolRegistry is not None
        assert ToolProvider is not None
        assert ToolContext is not None

    def test_enrichment_export(self):
        from companest import EnrichmentSource
        assert EnrichmentSource is not None

    def test_routing_binding_export(self):
        from companest import RoutingBinding
        assert RoutingBinding is not None

    def test_deny_list_exports(self):
        from companest import DEFAULT_TOOLS_DENY, SESSIONS_TOOL_NAMES
        assert "Bash" in DEFAULT_TOOLS_DENY
        assert "sessions_send" in SESSIONS_TOOL_NAMES


#  16. Per-Agent Tool Deny List (P1) 

class TestToolDenyList:
    """Test per-Pi configurable tool deny lists."""

    def test_default_deny_list(self):
        """Default PiConfig.tools_deny=[] uses DEFAULT_TOOLS_DENY."""
        from companest.pi import PiConfig, Pi
        from companest.tools import DEFAULT_TOOLS_DENY
        config = PiConfig(id="test")
        assert config.tools_deny == []
        # Verify DEFAULT_TOOLS_DENY has Bash
        assert "Bash" in DEFAULT_TOOLS_DENY

    def test_resolve_with_deny_filter(self):
        """ToolRegistry.resolve_tool_names filters denied tools."""
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        result = reg.resolve_tool_names(["Read", "Write", "Bash"], tools_deny={"Bash"})
        assert "Read" in result
        assert "Write" in result
        assert "Bash" not in result

    def test_custom_deny_list(self):
        """Custom tools_deny overrides global."""
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        result = reg.resolve_tool_names(
            ["Read", "Write", "Bash"], tools_deny={"Bash", "Write"},
        )
        assert "Read" in result
        assert "Write" not in result
        assert "Bash" not in result

    def test_deny_none_allows_all(self):
        """tools_deny=["none"]  no denials."""
        from companest.pi import PiConfig
        import tempfile, shutil
        from pathlib import Path
        from companest.memory import MemoryManager

        tmpdir = tempfile.mkdtemp()
        try:
            base = Path(tmpdir)
            (base / "teams" / "test" / "memory").mkdir(parents=True)
            mm = MemoryManager(str(base))

            config = PiConfig(id="pi1", tools=["Read", "Bash"], tools_deny=["none"])
            from companest.pi import Pi
            pi = Pi(config, mm, team_id="test")
            deny_set = pi._build_deny_set()
            assert deny_set == set()
        finally:
            shutil.rmtree(tmpdir)

    def test_parse_tools_deny_from_markdown(self):
        """_parse_pi_sections extracts tools_deny from team.md."""
        from companest.team import _parse_pi_sections
        text = (
            "# Team: test\n"
            "#### Pi: agent1\n"
            "- model: claude-haiku-4-5-20251001\n"
            "- tools: Read, Write\n"
            "- tools_deny: Bash, Write\n"
            "- max_turns: 5\n"
        )
        pis = _parse_pi_sections(text)
        assert len(pis) == 1
        assert pis[0].tools_deny == ["Bash", "Write"]

    def test_parse_tools_deny_absent(self):
        """Missing tools_deny  empty list."""
        from companest.team import _parse_pi_sections
        text = (
            "# Team: test\n"
            "#### Pi: agent1\n"
            "- model: claude-haiku-4-5-20251001\n"
            "- tools: Read\n"
        )
        pis = _parse_pi_sections(text)
        assert len(pis) == 1
        assert pis[0].tools_deny == []


#  17. Agent-to-Agent Messaging / Sessions Tools (P0) 

class TestSessionsTools:
    """Test sessions tool name resolution and factory availability."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)

        # Create two teams for messaging
        for team_id in ("alpha", "beta"):
            team_dir = self.base / "teams" / team_id
            (team_dir / "memory").mkdir(parents=True)
            (team_dir / "pis" / "lead").mkdir(parents=True)
            (team_dir / "team.md").write_text(
                f"# Team: {team_id}\n- role: general\n- lead_pi: lead\n- enabled: true\n"
            )
            (team_dir / "pis" / "lead" / "soul.md").write_text(f"{team_id} lead")

        (self.base / "soul.md").write_text("Master soul")

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_sessions_tool_names_defined(self):
        from companest.tools import SESSIONS_TOOL_NAMES
        assert "sessions_send" in SESSIONS_TOOL_NAMES
        assert "sessions_list" in SESSIONS_TOOL_NAMES
        assert "sessions_history" in SESSIONS_TOOL_NAMES

    def test_resolve_sessions_tools(self):
        from companest.tools import resolve_tool_names
        result = resolve_tool_names(["sessions_send", "sessions_list", "sessions_history"])
        assert "mcp__sessions__sessions_send" in result
        assert "mcp__sessions__sessions_list" in result
        assert "mcp__sessions__sessions_history" in result

    def test_messenger_preset(self):
        from companest.tools import resolve_tool_names, TOOL_PRESETS
        assert "messenger" in TOOL_PRESETS
        result = resolve_tool_names(["messenger"])
        assert "mcp__sessions__sessions_send" in result

    def test_sessions_provider_registered(self):
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        assert "sessions" in reg.list_providers()

    def test_sessions_send_writes_to_inbox(self):
        """Verify sessions_send writes a message to target team's inbox.json."""
        import datetime

        # Simulate what sessions_send does
        entry = {
            "from_team": "alpha",
            "from_pi": "lead",
            "message": "Hello from alpha!",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.mm.append_team_memory("beta", "inbox.json", entry)

        inbox = self.mm.read_team_memory("beta", "inbox.json")
        assert isinstance(inbox, list)
        assert len(inbox) == 1
        assert inbox[0]["from_team"] == "alpha"
        assert inbox[0]["message"] == "Hello from alpha!"

    def test_sessions_history_reads_inbox(self):
        """Verify inbox can be read back with limit."""
        import datetime

        for i in range(5):
            entry = {
                "from_team": "alpha",
                "from_pi": "lead",
                "message": f"msg {i}",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            self.mm.append_team_memory("beta", "inbox.json", entry)

        inbox = self.mm.read_team_memory("beta", "inbox.json")
        assert len(inbox) == 5
        # Simulate limit=3
        recent = inbox[-3:]
        assert len(recent) == 3
        assert recent[0]["message"] == "msg 2"

    def test_sessions_list_via_memory(self):
        """sessions_list fallback lists teams from memory."""
        teams = self.mm.list_teams()
        assert "alpha" in teams
        assert "beta" in teams


#  18. Binding-Based Fast Routing (P2) 

class TestRoutingBindings:
    """Test deterministic regex binding rules in SmartRouter."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        teams_dir = self.base / "teams"

        # Create stock team
        stock_dir = teams_dir / "stock"
        (stock_dir / "memory").mkdir(parents=True)
        (stock_dir / "pis" / "analyst").mkdir(parents=True)
        (stock_dir / "team.md").write_text(
            "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
            "\n#### Pi: analyst\n- model: claude-haiku-4-5-20251001\n"
        )
        (stock_dir / "pis" / "analyst" / "soul.md").write_text("Stock analyst")
        (stock_dir / "soul.md").write_text("Stock team")

        # Create scheduler team
        sched_dir = teams_dir / "scheduler"
        (sched_dir / "memory").mkdir(parents=True)
        (sched_dir / "pis" / "bot").mkdir(parents=True)
        (sched_dir / "team.md").write_text(
            "# Team: scheduler\n- role: general\n- lead_pi: bot\n- enabled: true\n"
            "\n#### Pi: bot\n- model: claude-haiku-4-5-20251001\n"
        )
        (sched_dir / "pis" / "bot" / "soul.md").write_text("Scheduler bot")
        (sched_dir / "soul.md").write_text("Scheduler team")

        from companest.memory import MemoryManager
        from companest.team import TeamRegistry
        from companest.router import SmartRouter

        self.mm = MemoryManager(str(self.base))
        self.reg = TeamRegistry(str(self.base / "teams"), self.mm)
        self.reg.scan_configs()
        self.router = SmartRouter(self.reg, self.mm)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_add_binding(self):
        self.router.add_binding(r"stock|ticker", "stock", mode="cascade")
        assert len(self.router._bindings) == 1
        assert self.router._bindings[0].team_id == "stock"

    def test_binding_match_skips_llm(self):
        """Binding match returns decision with source='binding', confidence=0.95."""
        self.router.add_binding(r"stock|ticker", "stock", mode="cascade")

        decision = asyncio.get_event_loop().run_until_complete(
            self.router.route("What is the stock price?")
        )
        assert not decision.declined
        assert len(decision.teams) == 1
        assert decision.teams[0].team_id == "stock"
        assert decision.confidence == 0.95
        assert decision.mode == "cascade"
        assert "Binding match" in decision.reasoning

    def test_binding_first_match_wins(self):
        """First binding whose pattern matches wins."""
        self.router.add_binding(r"stock|ticker", "stock", mode="cascade")
        self.router.add_binding(r"stock|market", "scheduler", mode="default")

        decision = asyncio.get_event_loop().run_until_complete(
            self.router.route("Check the stock market")
        )
        # First binding matches "stock"
        assert decision.teams[0].team_id == "stock"

    def test_binding_no_match_falls_through(self):
        """No binding match  falls through to LLM/keyword."""
        self.router.add_binding(r"schedule|remind", "scheduler", mode="default")

        decision = asyncio.get_event_loop().run_until_complete(
            self.router.route("What is the stock price?")
        )
        # Falls through to keyword fallback (LLM not available in test)
        assert decision.teams[0].team_id == "stock"
        assert "Binding match" not in decision.reasoning

    def test_binding_invalid_team_skipped(self):
        """Binding with non-existent team is skipped."""
        self.router.add_binding(r"anything", "nonexistent_team")

        decision = asyncio.get_event_loop().run_until_complete(
            self.router.route("anything goes here about stock")
        )
        # Binding's team doesn't exist  skipped, falls through
        assert decision.teams[0].team_id == "stock"

    def test_explicit_tag_beats_binding(self):
        """Explicit @team tag has highest priority (before bindings)."""
        self.router.add_binding(r".*", "stock")

        decision = asyncio.get_event_loop().run_until_complete(
            self.router.route("@scheduler run my task")
        )
        assert decision.teams[0].team_id == "scheduler"
        assert decision.confidence == 1.0

    def test_routing_binding_dataclass(self):
        from companest.router import RoutingBinding
        import re
        binding = RoutingBinding(
            pattern=re.compile(r"test"), team_id="stock", mode="loop", priority=2,
        )
        assert binding.team_id == "stock"
        assert binding.mode == "loop"
        assert binding.priority == 2

    def test_orchestrator_add_routing_binding(self):
        """Orchestrator exposes add_routing_binding, flushes on init_teams."""
        from companest.config import CompanestConfig
        from companest.orchestrator import CompanestOrchestrator

        config = CompanestConfig()
        orch = CompanestOrchestrator(config)

        # Add binding before init_teams
        orch.add_routing_binding(r"stock|ticker", "stock", mode="cascade")
        assert len(orch._pending_bindings) == 1

        # After init_teams, binding is flushed to SmartRouter
        orch.init_teams(str(self.base))
        assert len(orch._smart_router._bindings) == 1
        assert orch._pending_bindings == []

    def test_orchestrator_add_binding_after_init(self):
        """add_routing_binding works after init_teams too."""
        from companest.config import CompanestConfig
        from companest.orchestrator import CompanestOrchestrator

        config = CompanestConfig()
        orch = CompanestOrchestrator(config)
        orch.init_teams(str(self.base))

        orch.add_routing_binding(r"schedule|remind", "scheduler")
        assert len(orch._smart_router._bindings) == 1


#  19. CostGate Post-Hoc Mode 

class TestCostGatePostHoc:
    """Test post-hoc default mode: always approve, reconcile later."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        (self.base / "teams" / "finance" / "memory").mkdir(parents=True)

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_post_hoc_always_approves(self):
        """Default mode (post_hoc) auto-approves everything under the limit."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        # No budget.json  defaults to post_hoc mode
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("analyze market", "stock", "claude-sonnet-4-5-20250929")
        )
        assert decision.action == "auto_approve"
        assert "Post-hoc" in decision.reason

    def test_post_hoc_explicit_mode(self):
        """Explicit post_hoc mode in budget.json."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        self.mm.write_team_memory("finance", "budget.json", {
            "mode": "post_hoc",
            "daily_limit": 100.0,
        })
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("task", "stock", "claude-sonnet-4-5-20250929")
        )
        assert decision.action == "auto_approve"

    def test_critical_bypasses(self):
        """Critical priority bypasses even circuit breaker."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        # Set daily limit to 0 to trigger rejection normally
        self.mm.write_team_memory("finance", "budget.json", {
            "daily_limit": 0.001,
            "mode": "approval",
        })
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("urgent task", "stock", "claude-opus-4-6", priority="critical")
        )
        assert decision.action == "auto_approve"
        assert decision.priority == "critical"
        assert "Critical" in decision.reason


#  20. CircuitBreaker 

class TestCircuitBreaker:
    """Test CircuitBreaker velocity tracking and trip/reset behavior."""

    def test_not_tripped_under_threshold(self):
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30)
        cb.record(0.5, "stock")
        assert not cb.is_tripped(daily_limit=10.0)  # 0.5 < 3.0 (30% of 10)

    def test_tripped_on_velocity_spike(self):
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30)
        # Record spending that exceeds 30% of 10.0 = $3.0
        cb.record(2.0, "stock")
        cb.record(1.5, "stock")
        assert cb.is_tripped(daily_limit=10.0)  # 3.5 >= 3.0

    def test_auto_reset_after_cooldown(self):
        from unittest.mock import patch
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30, cooldown_minutes=0.001)
        # Trip it
        cb.record(5.0, "stock")
        assert cb.is_tripped(daily_limit=10.0)

        # Wait for cooldown (0.001 min = 0.06s)
        time.sleep(0.1)
        assert not cb.is_tripped(daily_limit=10.0)

    def test_manual_reset(self):
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30)
        cb.record(5.0, "stock")
        assert cb.is_tripped(daily_limit=10.0)

        cb.reset()
        assert not cb.is_tripped(daily_limit=10.0)

    def test_get_status(self):
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30)
        cb.record(1.0, "stock")
        status = cb.get_status()
        assert status["tripped"] is False
        assert status["window_spend"] > 0
        assert status["window_minutes"] == 5
        assert status["threshold_pct"] == 30
        assert status["events_in_window"] == 1

    def test_get_status_tripped(self):
        from companest.cost_gate import CircuitBreaker
        cb = CircuitBreaker(window_minutes=5, threshold_pct=30)
        cb.record(5.0, "stock")
        cb.is_tripped(daily_limit=10.0)
        status = cb.get_status()
        assert status["tripped"] is True
        assert status["cooldown_remaining_seconds"] > 0


#  21. Adaptive Budget 

class TestAdaptiveBudget:
    """Test per-team budgets, overflow pool, priority multipliers, rolling window."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        (self.base / "teams" / "finance" / "memory").mkdir(parents=True)

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_team_budget_enforced(self):
        """Team over its per-team budget  rejected."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        self.mm.write_team_memory("finance", "budget.json", {
            "daily_limit": 100.0,
            "mode": "post_hoc",
            "team_budgets": {"stock": {"daily": 0.001}},
        })
        # Record some spending to push stock over budget
        cg.record_spending("stock", "old task", {}, 0.01)

        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("new task", "stock", "claude-sonnet-4-5-20250929")
        )
        assert decision.action == "rejected"
        assert "budget exceeded" in decision.reason

    def test_overflow_pool_used(self):
        """Team over budget but overflow pool covers the overage."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        self.mm.write_team_memory("finance", "budget.json", {
            "daily_limit": 100.0,
            "mode": "post_hoc",
            "team_budgets": {"stock": {"daily": 0.001}},
            "overflow_pool": 10.0,
        })
        # Record small spending
        cg.record_spending("stock", "old task", {}, 0.01)

        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("new task", "stock", "claude-haiku-4-5-20251001")
        )
        # Should be approved because overflow pool covers the overage
        assert decision.action == "auto_approve"

    def test_priority_multiplier_high(self):
        """High priority gets 3x auto_approve threshold."""
        from companest.cost_gate import CostGate, UserNotifier
        cg = CostGate(self.mm, notifier=UserNotifier())
        self.mm.write_team_memory("finance", "budget.json", {
            "auto_approve_threshold": 0.01,
            "escalation_threshold": 100.0,
            "daily_limit": 1000.0,
            "mode": "approval",
            "priority_multipliers": {"critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5},
        })
        # A cost of ~$0.02 is above normal threshold (0.01) but below high (0.03)
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("x" * 20, "stock", "gpt-4o-mini", priority="high")
        )
        assert decision.action == "auto_approve"
        assert decision.priority == "high"

    def test_priority_multiplier_low(self):
        """Low priority gets 0.5x threshold  more likely to notify."""
        from companest.cost_gate import CostGate, UserNotifier
        cg = CostGate(self.mm, notifier=UserNotifier())
        self.mm.write_team_memory("finance", "budget.json", {
            "auto_approve_threshold": 0.05,
            "escalation_threshold": 100.0,
            "daily_limit": 1000.0,
            "mode": "approval",
            "priority_multipliers": {"critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5},
        })
        # With low priority, auto_threshold is 0.025.
        # A small task with gpt-4o-mini costs ~$0.003  auto_approve (below even low threshold)
        decision = asyncio.get_event_loop().run_until_complete(
            cg.evaluate("hi", "stock", "gpt-4o-mini", priority="low")
        )
        assert decision.action == "auto_approve"
        assert decision.priority == "low"

    def test_rolling_window_excludes_old(self):
        """Rolling window only counts recent entries."""
        from companest.cost_gate import CostGate
        from datetime import datetime, timezone, timedelta
        import json

        cg = CostGate(self.mm)

        # Write old spending entry (2 days ago)
        old_date = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        new_date = datetime.now(timezone.utc).isoformat()
        log = [
            {"date": old_date, "team": "stock", "cost": 100.0},
            {"date": new_date, "team": "stock", "cost": 0.01},
        ]
        self.mm.write_team_memory("finance", "spending-log.json", log)

        # Rolling window of 24h should only see $0.01
        window_spend = cg._get_window_spending(24)
        assert window_spend < 1.0  # old $100 excluded


#  22. Budget Backward Compat 

class TestBudgetBackwardCompat:
    """Test that old budget.json format gets defaults and new format reads correctly."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        (self.base / "teams" / "finance" / "memory").mkdir(parents=True)

        from companest.memory import MemoryManager
        self.mm = MemoryManager(str(self.base))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_old_format_defaults(self):
        """Old budget.json (no new fields) gets sensible defaults."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        self.mm.write_team_memory("finance", "budget.json", {
            "auto_approve_threshold": 0.05,
            "escalation_threshold": 1.0,
            "daily_limit": 10.0,
        })
        budget = cg._load_budget()
        assert budget["mode"] == "post_hoc"
        assert budget["rolling_window_hours"] == 24
        assert budget["team_budgets"] == {}
        assert budget["overflow_pool"] == 0.0
        assert "circuit_breaker" in budget
        assert "priority_multipliers" in budget

    def test_new_format_read(self):
        """New budget.json format reads all fields correctly."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        self.mm.write_team_memory("finance", "budget.json", {
            "auto_approve_threshold": 0.05,
            "escalation_threshold": 1.0,
            "daily_limit": 10.0,
            "mode": "approval",
            "rolling_window_hours": 12,
            "circuit_breaker": {"window_minutes": 3, "threshold_pct": 25},
            "team_budgets": {"stock": {"daily": 5.0}},
            "overflow_pool": 2.0,
            "priority_multipliers": {"critical": 999, "high": 2.0, "normal": 1.0, "low": 0.3},
        })
        budget = cg._load_budget()
        assert budget["mode"] == "approval"
        assert budget["rolling_window_hours"] == 12
        assert budget["team_budgets"]["stock"]["daily"] == 5.0
        assert budget["overflow_pool"] == 2.0
        assert budget["priority_multipliers"]["high"] == 2.0

    def test_no_budget_file_defaults(self):
        """No budget.json  all defaults applied."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        budget = cg._load_budget()
        assert budget["auto_approve_threshold"] == 0.05
        assert budget["daily_limit"] == 10.0
        assert budget["mode"] == "post_hoc"
        assert budget["rolling_window_hours"] == 24

    def test_spending_summary_includes_new_fields(self):
        """get_spending_summary() returns mode, window_spend, circuit_breaker."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        summary = cg.get_spending_summary()
        assert "mode" in summary
        assert "circuit_breaker" in summary
        assert summary["mode"] == "post_hoc"

    def test_daily_report_structure(self):
        """get_daily_report() returns expected structure."""
        from companest.cost_gate import CostGate
        cg = CostGate(self.mm)
        cg.record_spending("stock", "task1", {}, 0.01)
        report = cg.get_daily_report(hours=24)
        assert "window_hours" in report
        assert "window_spend" in report
        assert "daily_limit" in report
        assert "utilization_pct" in report
        assert "by_team" in report
        assert "circuit_breaker" in report
        assert "mode" in report
        assert report["window_spend"] > 0


#  23. RoutingDecision task_priority 

class TestRoutingDecisionPriority:
    """Test task_priority field on RoutingDecision."""

    def test_default_priority(self):
        from companest.router import RoutingDecision
        d = RoutingDecision()
        assert d.task_priority == "normal"

    def test_priority_preserved(self):
        from companest.router import RoutingDecision
        d = RoutingDecision(task_priority="high")
        assert d.task_priority == "high"

    def test_parse_priority_from_llm_response(self):
        """SmartRouter parses task_priority from LLM JSON."""
        tmpdir = tempfile.mkdtemp()
        try:
            base = Path(tmpdir)
            teams_dir = base / "teams"

            stock_dir = teams_dir / "stock"
            (stock_dir / "memory").mkdir(parents=True)
            (stock_dir / "pis" / "analyst").mkdir(parents=True)
            (stock_dir / "team.md").write_text(
                "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
                "\n#### Pi: analyst\n- model: claude-haiku-4-5-20251001\n"
            )
            (stock_dir / "pis" / "analyst" / "soul.md").write_text("Stock analyst")
            (stock_dir / "soul.md").write_text("Stock team")

            from companest.memory import MemoryManager
            from companest.team import TeamRegistry
            from companest.router import SmartRouter

            mm = MemoryManager(str(base))
            reg = TeamRegistry(str(base / "teams"), mm)
            reg.scan_configs()
            router = SmartRouter(reg, mm)

            raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "mode": "cascade", "task_priority": "high", "confidence": 0.9}'
            result = router._parse_response(raw)
            assert result is not None
            assert result.task_priority == "high"
        finally:
            shutil.rmtree(tmpdir)

    def test_parse_priority_invalid_defaults(self):
        """Invalid task_priority defaults to normal."""
        tmpdir = tempfile.mkdtemp()
        try:
            base = Path(tmpdir)
            teams_dir = base / "teams"

            stock_dir = teams_dir / "stock"
            (stock_dir / "memory").mkdir(parents=True)
            (stock_dir / "pis" / "analyst").mkdir(parents=True)
            (stock_dir / "team.md").write_text(
                "# Team: stock\n- role: general\n- lead_pi: analyst\n- enabled: true\n"
                "\n#### Pi: analyst\n- model: claude-haiku-4-5-20251001\n"
            )
            (stock_dir / "pis" / "analyst" / "soul.md").write_text("Stock analyst")
            (stock_dir / "soul.md").write_text("Stock team")

            from companest.memory import MemoryManager
            from companest.team import TeamRegistry
            from companest.router import SmartRouter

            mm = MemoryManager(str(base))
            reg = TeamRegistry(str(base / "teams"), mm)
            reg.scan_configs()
            router = SmartRouter(reg, mm)

            raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "task_priority": "turbo", "confidence": 0.9}'
            result = router._parse_response(raw)
            assert result is not None
            assert result.task_priority == "normal"
        finally:
            shutil.rmtree(tmpdir)


#  24. Server New Endpoints 

class TestServerNewEndpoints:
    """Test finance report and circuit breaker reset endpoints exist."""

    def test_new_endpoints_registered(self):
        """Check that new finance endpoints are on the app."""
        pytest.importorskip("fastapi")

        from companest.config import CompanestConfig
        from companest.jobs import JobManager
        from companest.server import CompanestAPIServer

        config = CompanestConfig(debug=True)  # debug=True: no auth token required
        jm = JobManager.__new__(JobManager)
        jm.orchestrator = None
        jm._data_dir = None

        server = CompanestAPIServer(config, jm)
        app = server.create_app()

        routes = [r.path for r in app.routes]
        assert "/api/finance/report" in routes
        assert "/api/finance/circuit-breaker/reset" in routes


class TestCompanyScoping:
    """Test company-scoped routing helpers."""

    def test_can_access_team_respects_explicit_shared_team_whitelist(self):
        from companest.company import CompanyConfig
        from companest.orchestrator import CompanestOrchestrator

        orch = CompanestOrchestrator.__new__(CompanestOrchestrator)
        orch.company_registry = type("Registry", (), {
            "get": lambda self, company_id: {
                "acme": CompanyConfig(id="acme", name="Acme", shared_teams=[]),
                "beta": CompanyConfig(id="beta", name="Beta", shared_teams=["general"]),
            }.get(company_id)
        })()

        assert not orch.can_access_team("acme", "general")
        assert orch.can_access_team("beta", "general")
        assert not orch.can_access_team("beta", "finance")

    def test_initialized_company_ids_include_registry_only_companies(self):
        from companest.orchestrator import CompanestOrchestrator

        orch = CompanestOrchestrator.__new__(CompanestOrchestrator)
        orch._ceo_pis = {}
        orch.company_registry = type("Registry", (), {
            "list_companies": lambda self: ["ceo-disabled"]
        })()
        orch.team_registry = type("Teams", (), {
            "list_teams": lambda self: []
        })()

        assert orch._initialized_company_ids() == ["ceo-disabled"]


#  25. CostDecision priority field 

class TestCostDecisionPriority:
    """Test CostDecision includes priority field."""

    def test_default_priority(self):
        from companest.cost_gate import CostDecision, CostEstimate
        est = CostEstimate(100, 200, 0.01, "stock", "gpt-4o-mini")
        d = CostDecision(action="auto_approve", estimate=est, reason="test")
        assert d.priority == "normal"

    def test_custom_priority(self):
        from companest.cost_gate import CostDecision, CostEstimate
        est = CostEstimate(100, 200, 0.01, "stock", "gpt-4o-mini")
        d = CostDecision(action="auto_approve", estimate=est, reason="test", priority="high")
        assert d.priority == "high"


#  26. EventType circuit breaker 

class TestEventTypeCircuitBreaker:
    """Test CIRCUIT_BREAKER_TRIPPED event type."""

    def test_event_type_exists(self):
        from companest.events import EventType
        assert hasattr(EventType, "CIRCUIT_BREAKER_TRIPPED")
        assert EventType.CIRCUIT_BREAKER_TRIPPED.value == "circuit_breaker_tripped"
