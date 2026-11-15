"""Companest Design Verification Tests"""

import asyncio
import time

import pytest

from companest.memory import MemoryManager


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_team(base, team_id, pi, *, role="general", extra="", pi_md="",
               team_soul=None, pi_soul="soul"):
    d = base / "teams" / team_id
    (d / "memory").mkdir(parents=True)
    (d / "pis" / pi).mkdir(parents=True)
    (d / "team.md").write_text(
        f"# Team: {team_id}\n- role: {role}\n- lead_pi: {pi}\n- enabled: true\n{extra}{pi_md}"
    )
    (d / "pis" / pi / "soul.md").write_text(pi_soul)
    if team_soul:
        (d / "soul.md").write_text(team_soul)


def _smart_router(base, teams):
    from companest.team import TeamRegistry
    from companest.router import SmartRouter

    for team_id, pi in teams:
        _make_team(
            base, team_id, pi,
            pi_md=f"\n#### Pi: {pi}\n- model: claude-haiku-4-5-20251001\n",
            team_soul=f"{team_id} team", pi_soul=f"{team_id} {pi}",
        )
    mm = MemoryManager(str(base))
    reg = TeamRegistry(str(base / "teams"), mm)
    reg.scan_configs()
    return SmartRouter(reg, mm)


class TestMemoryPrompts:
    """MemoryManager soul/user reads and system prompt enrichment."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        _make_team(tmp_path, "stock", "analyst", team_soul="Stock team soul",
                   pi_soul="You are a stock analyst.")
        _make_team(tmp_path, "research", "scout", role="research",
                   extra="- always_on: true\n", team_soul="Research team soul",
                   pi_soul="You are a research scout.")
        _make_team(tmp_path, "data", "collector", role="data", pi_soul="Data collector.")
        (tmp_path / "soul.md").write_text("Master soul")
        (tmp_path / "user.md").write_text("# User\n- Language: English")
        self.mm = MemoryManager(str(tmp_path))

    def _write_briefing(self, items):
        self.mm.write_team_memory("research", "briefing.json", {
            "updated_at": "2025-01-15T10:30:00Z",
            "items": items,
        })

    def test_read_master_and_pi_files(self):
        assert "Master soul" in self.mm.read_master_soul()
        assert "English" in self.mm.read_master_user()
        assert "analyst" in self.mm.read_pi_soul("stock", "analyst").lower()

    def test_list_teams_and_team_exists(self):
        assert "stock" in self.mm.list_teams()
        assert self.mm.team_exists("stock")
        assert not self.mm.team_exists("nonexistent")

    def test_build_system_prompt(self):
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "stock analyst" in prompt.lower()
        assert "English" in prompt
        assert "World Briefing" not in prompt

    def test_briefing_injected_into_business_team(self):
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
        self._write_briefing([
            {"headline": f"Item {i}", "category": "world", "source": "AP"}
            for i in range(20)
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "Item 14" in prompt
        assert "Item 15" not in prompt

    def test_empty_items_no_section(self):
        self._write_briefing([])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_malformed_briefing_no_crash(self):
        self.mm.write_team_memory("research", "briefing.json", "not a dict")
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_items_missing_headline_skipped(self):
        self._write_briefing([
            {"category": "world", "source": "AP"},
            {"headline": "Valid headline", "category": "tech", "source": "BBC"},
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "Valid headline" in prompt
        briefing_section = prompt.split("World Briefing")[1]
        assert briefing_section.count("- ") == 1

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

        self.mm.register_enrichment(EnrichmentSource(
            source_team_id="data",
            memory_key="stuff.json",
            section_title="Custom",
            formatter=lambda data: "## Custom Section\nSome data",
            exclude_teams={"data"},
        ))
        self.mm.write_team_memory("data", "stuff.json", {"anything": True})

        prompt = self.mm.build_system_prompt("data", "collector")
        assert "Custom Section" not in prompt


class TestTeamRegistry:
    """TeamConfig parsing and TeamRegistry lifecycle."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from companest.team import TeamRegistry

        self.base = tmp_path
        _make_team(
            tmp_path, "stock", "analyst", extra="- always_on: false\n",
            pi_md="\n#### Pi: analyst\n- model: claude-sonnet-4-5-20250929\n"
                  "- tools: memory_read, web_search\n- max_turns: 5\n",
        )
        _make_team(
            tmp_path, "finance", "accountant", role="cost_gate", extra="- always_on: true\n",
            pi_md="\n#### Pi: accountant\n- model: claude-haiku-4-5-20251001\n"
                  "- tools: memory_read, memory_write\n- max_turns: 3\n",
        )
        self.mm = MemoryManager(str(tmp_path))
        self.reg = TeamRegistry(str(tmp_path / "teams"), self.mm)
        self.reg.scan_configs()

    def test_team_config_from_markdown(self):
        from companest.team import TeamConfig
        config = TeamConfig.from_markdown(self.base / "teams" / "stock" / "team.md")
        assert config.id == "stock"
        assert config.role == "general"
        assert config.lead_pi == "analyst"
        assert not config.always_on
        assert len(config.pis) == 1
        assert config.pis[0].id == "analyst"
        assert config.pis[0].model == "claude-sonnet-4-5-20250929"

    def test_team_config_discovers_pis_from_directories(self):
        """team.md without Pi sections falls back to the pis/ subdirectories."""
        from companest.team import TeamConfig
        _make_team(self.base, "research", "scout", role="research", extra="- always_on: true\n")
        config = TeamConfig.from_markdown(self.base / "teams" / "research" / "team.md")
        assert config.id == "research"
        assert config.role == "research"
        assert config.always_on is True
        assert config.lead_pi == "scout"
        assert [p.id for p in config.pis] == ["scout"]

    def test_registry_scan(self):
        assert "stock" in self.reg.list_teams()
        assert "finance" in self.reg.list_teams()
        assert "finance" in self.reg.list_meta_teams()
        assert "stock" not in self.reg.list_meta_teams()
        assert "finance" in self.reg.list_active()

    def test_registry_on_demand_create(self):
        assert "stock" not in self.reg.list_active()
        team = self.reg.get_or_create("stock")
        assert team.id == "stock"
        assert "stock" in self.reg.list_active()

    def test_registry_evict_idle(self):
        from companest.team import TeamRegistry
        reg = TeamRegistry(str(self.base / "teams"), self.mm, idle_timeout=0)
        reg.scan_configs()

        reg.get_or_create("stock")
        assert "stock" in reg.list_active()

        time.sleep(0.01)
        evicted = reg.evict_idle()
        assert "stock" in evicted
        assert "finance" not in evicted
        assert "finance" in reg.list_active()

    def test_registry_get_nonexistent_raises(self):
        from companest.exceptions import TeamError
        with pytest.raises(TeamError):
            self.reg.get_or_create("nonexistent")

    def test_fleet_status(self):
        status = self.reg.get_fleet_status()
        assert "stock" in status["registered"]
        assert "finance" in status["meta"]

    def test_register_on_demand_team(self):
        from companest.team import TeamConfig
        from companest.pi import PiConfig

        self.reg.register(TeamConfig(
            id="kalshi",
            role="trading",
            lead_pi="trader",
            pis=[PiConfig(id="trader", model="claude-haiku-4-5-20251001", tools=["memory_read"])],
        ))

        assert "kalshi" in self.reg.list_teams()
        assert self.reg.get_or_create("kalshi").id == "kalshi"

    def test_register_always_on_team(self):
        from companest.team import TeamConfig
        from companest.pi import PiConfig

        self.reg.register(TeamConfig(
            id="monitor",
            role="monitoring",
            lead_pi="watcher",
            pis=[PiConfig(id="watcher")],
        ), always_on=True)

        assert "monitor" in self.reg.list_meta_teams()
        assert "monitor" in self.reg.list_active()

    def test_register_disabled_team_skipped(self):
        from companest.team import TeamConfig
        self.reg.register(TeamConfig(id="disabled", enabled=False))
        assert "disabled" not in self.reg.list_teams()


class TestCostGate:
    """Cost approval tiers, per-team budgets, overflow pool, priorities, reports."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from companest.cost_gate import CostGate

        (tmp_path / "teams" / "finance" / "memory").mkdir(parents=True)
        self.mm = MemoryManager(str(tmp_path))
        self.cg = CostGate(self.mm)

    def _budget(self, **budget):
        self.mm.write_team_memory("finance", "budget.json", budget)

    def _notifying_gate(self):
        from companest.cost_gate import CostGate, UserNotifier
        return CostGate(self.mm, notifier=UserNotifier())

    def test_cost_estimate(self):
        est = self.cg.estimate_cost("short task", "claude-haiku-4-5-20251001", "stock")
        assert est.estimated_cost_usd > 0
        assert est.target_model == "claude-haiku-4-5-20251001"
        # Haiku is the cheapest Anthropic model: no same-provider downgrade
        assert est.suggested_downgrade is None

    def test_tier1_auto_approve(self):
        decision = _run(self.cg.evaluate("hi", "stock", "gpt-4o-mini"))
        assert decision.action == "auto_approve"
        assert decision.estimate.estimated_cost_usd < 0.05

    def test_tier2_notify_approve(self):
        cg = self._notifying_gate()
        self._budget(
            auto_approve_threshold=0.0001, escalation_threshold=100.0,
            daily_limit=1000.0, mode="approval",
        )
        decision = _run(cg.evaluate("analyze this stock", "stock", "claude-haiku-4-5-20251001"))
        assert decision.action == "notify_approve"

    def test_record_spending(self):
        self.cg.record_spending("stock", "test task", {"input": 100, "output": 200}, 0.001)
        log = self.mm.read_team_memory("finance", "spending-log.json")
        assert len(log) == 1
        assert log[0]["team"] == "stock"

    def test_spending_summary(self):
        self.cg.record_spending("stock", "t1", {}, 0.01)
        self.cg.record_spending("bizdev", "t2", {}, 0.02)

        summary = self.cg.get_spending_summary()
        assert summary["total"] == 0.03
        assert "stock" in summary["by_team"]
        assert summary["entries"] == 2

    def test_spending_summary_without_log(self):
        summary = self.cg.get_spending_summary()
        assert "circuit_breaker" in summary
        assert summary["mode"] == "post_hoc"

    def test_daily_report_structure(self):
        self.cg.record_spending("stock", "task1", {}, 0.01)
        report = self.cg.get_daily_report(hours=24)
        for key in ("window_hours", "window_spend", "daily_limit", "utilization_pct",
                    "by_team", "circuit_breaker", "mode"):
            assert key in report
        assert report["window_spend"] > 0

    def test_model_prices_complete(self):
        from companest.cost_gate import MODEL_PRICES
        from companest.cascade import get_downgrade
        for model_id in MODEL_PRICES:
            downgrade = get_downgrade(model_id)
            if downgrade is not None:
                assert downgrade in MODEL_PRICES, f"downgrade target {downgrade} not in MODEL_PRICES"

    def test_post_hoc_always_approves(self):
        """No budget.json: defaults to post_hoc and approves under the limit."""
        decision = _run(self.cg.evaluate("analyze market", "stock", "claude-sonnet-4-5-20250929"))
        assert decision.action == "auto_approve"
        assert "Post-hoc" in decision.reason

    def test_post_hoc_explicit_mode(self):
        self._budget(mode="post_hoc", daily_limit=100.0)
        decision = _run(self.cg.evaluate("task", "stock", "claude-sonnet-4-5-20250929"))
        assert decision.action == "auto_approve"

    def test_critical_bypasses(self):
        """Critical priority is approved even when the daily limit would reject."""
        self._budget(daily_limit=0.001, mode="approval")
        decision = _run(
            self.cg.evaluate("urgent task", "stock", "claude-opus-4-6", priority="critical")
        )
        assert decision.action == "auto_approve"
        assert decision.priority == "critical"
        assert "Critical" in decision.reason

    def test_team_budget_enforced(self):
        self._budget(
            daily_limit=100.0, mode="post_hoc", team_budgets={"stock": {"daily": 0.001}},
        )
        self.cg.record_spending("stock", "old task", {}, 0.01)

        decision = _run(self.cg.evaluate("new task", "stock", "claude-sonnet-4-5-20250929"))
        assert decision.action == "rejected"
        assert "budget exceeded" in decision.reason

    def test_overflow_pool_used(self):
        """Team over budget but overflow pool covers the overage."""
        self._budget(
            daily_limit=100.0, mode="post_hoc", team_budgets={"stock": {"daily": 0.001}},
            overflow_pool=10.0,
        )
        self.cg.record_spending("stock", "old task", {}, 0.01)

        decision = _run(self.cg.evaluate("new task", "stock", "claude-haiku-4-5-20251001"))
        assert decision.action == "auto_approve"

    def test_priority_multiplier(self):
        cg = self._notifying_gate()
        self._budget(
            auto_approve_threshold=0.01, escalation_threshold=100.0,
            daily_limit=1000.0, mode="approval",
            priority_multipliers={"critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5},
        )
        decision = _run(cg.evaluate("x" * 20, "stock", "gpt-4o-mini", priority="high"))
        assert decision.action == "auto_approve"
        assert decision.priority == "high"

    def test_rolling_window_excludes_old(self):
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        self.mm.write_team_memory("finance", "spending-log.json", [
            {"date": (now - timedelta(hours=48)).isoformat(), "team": "stock", "cost": 100.0},
            {"date": now.isoformat(), "team": "stock", "cost": 0.01},
        ])
        assert self.cg._get_window_spending(24) < 1.0

    def test_old_format_defaults(self):
        """Old budget.json (no new fields) gets sensible defaults."""
        self._budget(auto_approve_threshold=0.05, escalation_threshold=1.0, daily_limit=10.0)
        budget = self.cg._load_budget()
        assert budget["mode"] == "post_hoc"
        assert budget["rolling_window_hours"] == 24
        assert budget["team_budgets"] == {}
        assert budget["overflow_pool"] == 0.0
        assert "circuit_breaker" in budget
        assert "priority_multipliers" in budget

    def test_new_format_read(self):
        self._budget(
            auto_approve_threshold=0.05, escalation_threshold=1.0, daily_limit=10.0,
            mode="approval", rolling_window_hours=12,
            circuit_breaker={"window_minutes": 3, "threshold_pct": 25},
            team_budgets={"stock": {"daily": 5.0}}, overflow_pool=2.0,
            priority_multipliers={"critical": 999, "high": 2.0, "normal": 1.0, "low": 0.3},
        )
        budget = self.cg._load_budget()
        assert budget["mode"] == "approval"
        assert budget["rolling_window_hours"] == 12
        assert budget["team_budgets"]["stock"]["daily"] == 5.0
        assert budget["overflow_pool"] == 2.0
        assert budget["priority_multipliers"]["high"] == 2.0

    def test_no_budget_file_defaults(self):
        budget = self.cg._load_budget()
        assert budget["auto_approve_threshold"] == 0.05
        assert budget["daily_limit"] == 10.0
        assert budget["mode"] == "post_hoc"
        assert budget["rolling_window_hours"] == 24


class TestCircuitBreaker:
    """CircuitBreaker velocity tracking and trip/reset behavior."""

    @staticmethod
    def _breaker(**kwargs):
        from companest.cost_gate import CircuitBreaker
        return CircuitBreaker(window_minutes=5, threshold_pct=30, **kwargs)

    def test_not_tripped_under_threshold(self):
        cb = self._breaker()
        cb.record(0.5, "stock")
        assert not cb.is_tripped(daily_limit=10.0)  # 0.5 < 3.0 (30% of 10)

    def test_tripped_on_velocity_spike(self):
        cb = self._breaker()
        cb.record(2.0, "stock")
        cb.record(1.5, "stock")
        assert cb.is_tripped(daily_limit=10.0)  # 3.5 >= 3.0

    def test_auto_reset_after_cooldown(self):
        cb = self._breaker(cooldown_minutes=0.001)
        cb.record(5.0, "stock")
        assert cb.is_tripped(daily_limit=10.0)

        time.sleep(0.1)  # cooldown is 0.06s
        assert not cb.is_tripped(daily_limit=10.0)

    def test_manual_reset(self):
        cb = self._breaker()
        cb.record(5.0, "stock")
        assert cb.is_tripped(daily_limit=10.0)

        cb.reset()
        assert not cb.is_tripped(daily_limit=10.0)

    def test_get_status(self):
        cb = self._breaker()
        cb.record(1.0, "stock")
        status = cb.get_status()
        assert status["tripped"] is False
        assert status["window_spend"] > 0
        assert status["window_minutes"] == 5
        assert status["threshold_pct"] == 30
        assert status["events_in_window"] == 1

    def test_get_status_tripped(self):
        cb = self._breaker()
        cb.record(5.0, "stock")
        cb.is_tripped(daily_limit=10.0)
        status = cb.get_status()
        assert status["tripped"] is True
        assert status["cooldown_remaining_seconds"] > 0


class TestRouter:
    """Keyword-based team routing."""

    def setup_method(self):
        from companest.router import TeamRouter
        self.router = TeamRouter(
            available_teams=["stock", "bizdev", "engineering", "philosophy", "science", "finance"]
        )

    @pytest.mark.parametrize("task,team", [
        ("Analyze TSLA recent stock trends", "stock"),
        ("I want to buy a house, any advice?", "bizdev"),
        ("Fix the bug in the Python API", "engineering"),
        ("What is free will? What is the nature of consciousness?", "philosophy"),
        ("What is wrong with this physics paper's experiment design?", "science"),
        ("@stock check AAPL", "stock"),
        ("#engineering deploy to prod", "engineering"),
        ("hello world", "philosophy"),  # default fallback
    ])
    def test_route(self, task, team):
        assert self.router.route(task) == team

    def test_no_default_returns_none(self):
        from companest.router import TeamRouter
        router = TeamRouter(available_teams=["stock"], default_team=None)
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
    """SmartRouter._parse_response() with various LLM outputs, and keyword fallback."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.router = _smart_router(tmp_path, [("stock", "analyst"), ("engineering", "coder")])

    def test_parse_valid_json(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "Analyze TSLA", "priority": 1}], "strategy": "single", "reasoning": "Stock task", "confidence": 0.9, "declined": false}'
        result = self.router._parse_response(raw)
        assert len(result.teams) == 1
        assert result.teams[0].team_id == "stock"
        assert result.strategy == "single"
        assert result.confidence == 0.9

    def test_parse_markdown_fenced_json(self):
        raw = '```json\n{"teams": [{"team_id": "engineering", "instruction": "Fix bug"}], "strategy": "single", "reasoning": "Code task", "confidence": 0.85}\n```'
        result = self.router._parse_response(raw)
        assert result.teams[0].team_id == "engineering"

    def test_parse_declined(self):
        raw = '{"teams": [], "declined": true, "decline_reason": "No team fits", "reasoning": "General chat"}'
        result = self.router._parse_response(raw)
        assert result.declined is True
        assert result.decline_reason == "No team fits"

    def test_parse_invalid_json(self):
        assert self.router._parse_response("This is not JSON at all") is None

    def test_parse_filters_unknown_teams(self):
        raw = '{"teams": [{"team_id": "nonexistent", "instruction": "x"}, {"team_id": "stock", "instruction": "y"}], "strategy": "single", "confidence": 0.8}'
        result = self.router._parse_response(raw)
        assert len(result.teams) == 1
        assert result.teams[0].team_id == "stock"

    def test_parse_multi_team(self):
        raw = '{"teams": [{"team_id": "stock", "instruction": "a", "priority": 1}, {"team_id": "engineering", "instruction": "b", "priority": 2}], "strategy": "parallel", "reasoning": "cross-domain", "confidence": 0.75}'
        result = self.router._parse_response(raw)
        assert len(result.teams) == 2
        assert result.strategy == "parallel"

    @pytest.mark.parametrize("field,value,expected", [
        ("mode", '"council"', "council"),
        ("mode", '"turbo"', "default"),
        ("mode", None, "default"),
        ("task_priority", '"high"', "high"),
        ("task_priority", '"turbo"', "normal"),
    ])
    def test_parse_mode_and_priority(self, field, value, expected):
        extra = f', "{field}": {value}' if value else ""
        raw = '{"teams": [{"team_id": "stock", "instruction": "x"}], "strategy": "single", "confidence": 0.8' + extra + "}"
        result = self.router._parse_response(raw)
        assert getattr(result, field) == expected

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
        assert prompt1 is prompt2

        self.router.invalidate_cache()
        assert self.router._build_system_prompt() is not prompt1


class TestRoutingBindings:
    """Deterministic regex binding rules in SmartRouter."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.router = _smart_router(tmp_path, [("stock", "analyst"), ("scheduler", "bot")])

    def test_binding_match_skips_llm(self):
        self.router.add_binding(r"stock|ticker", "stock", mode="cascade")

        decision = _run(self.router.route("What is the stock price?"))
        assert not decision.declined
        assert len(decision.teams) == 1
        assert decision.teams[0].team_id == "stock"
        assert decision.confidence == 0.95
        assert decision.mode == "cascade"
        assert "Binding match" in decision.reasoning

    def test_binding_first_match_wins(self):
        self.router.add_binding(r"stock|ticker", "stock", mode="cascade")
        self.router.add_binding(r"stock|market", "scheduler", mode="default")

        decision = _run(self.router.route("Check the stock market"))
        assert decision.teams[0].team_id == "stock"

    def test_binding_no_match_falls_through(self):
        """No binding match: falls through to keyword fallback (no LLM in tests)."""
        self.router.add_binding(r"schedule|remind", "scheduler", mode="default")

        decision = _run(self.router.route("What is the stock price?"))
        assert decision.teams[0].team_id == "stock"
        assert "Binding match" not in decision.reasoning

    def test_binding_invalid_team_skipped(self):
        self.router.add_binding(r"anything", "nonexistent_team")

        decision = _run(self.router.route("anything goes here about stock"))
        assert decision.teams[0].team_id == "stock"

    def test_explicit_tag_beats_binding(self):
        self.router.add_binding(r".*", "stock")

        decision = _run(self.router.route("@scheduler run my task"))
        assert decision.teams[0].team_id == "scheduler"
        assert decision.confidence == 1.0


class TestScheduler:
    """Scheduler lifecycle."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from companest.scheduler import Scheduler

        self.calls = 0

        async def task():
            self.calls += 1

        self.s = Scheduler()
        self.s.add("test_task", task, interval=3600)

    def test_add_and_status(self):
        status = self.s.get_status()
        assert not status["started"]
        assert "test_task" in status["tasks"]
        assert status["tasks"]["test_task"]["interval_seconds"] == 3600

    def test_start_stop(self):
        async def lifecycle():
            await self.s.start()
            assert self.s.get_status()["started"]
            assert self.s.get_status()["tasks"]["test_task"]["running"]
            await self.s.stop()
            assert not self.s.get_status()["started"]

        _run(lifecycle())

    def test_remove(self):
        assert self.s.remove("test_task")
        assert "test_task" not in self.s._tasks

    def test_run_now(self):
        _run(self.s.run_now("test_task"))
        assert self.calls == 1


class TestTools:
    """Tool name resolution, presets, registry providers and deny lists."""

    @pytest.mark.parametrize("config,expected", [
        (["web_search", "exec"], {"WebSearch", "Bash"}),
        (["memory_read", "memory_write"], {"mcp__mem__memory_read", "mcp__mem__memory_write"}),
        (["researcher"], {"WebSearch", "mcp__mem__memory_read"}),
        (["messenger"], {"mcp__sessions__sessions_send"}),
        (["sessions_send", "sessions_list", "sessions_history"], {
            "mcp__sessions__sessions_send",
            "mcp__sessions__sessions_list",
            "mcp__sessions__sessions_history",
        }),
    ])
    def test_resolve_contains(self, config, expected):
        from companest.tools import resolve_tool_names
        assert expected <= set(resolve_tool_names(config))

    def test_resolve_builtin_keeps_order(self):
        from companest.tools import resolve_tool_names
        assert resolve_tool_names(["Read", "Write", "Bash"]) == ["Read", "Write", "Bash"]

    def test_deduplication(self):
        from companest.tools import resolve_tool_names
        assert resolve_tool_names(["Read", "Read", "read"]).count("Read") == 1

    def test_builtin_providers_registered(self):
        from companest.tools import ToolRegistry
        providers = ToolRegistry().list_providers()
        for name in ("memory", "scheduler", "feed", "sessions"):
            assert name in providers

    def test_register_and_unregister_provider(self):
        from companest.tools import ToolRegistry, ToolProvider
        reg = ToolRegistry()
        reg.register(ToolProvider(name="custom", tool_names={"foo"}))
        assert "custom" in reg.list_providers()
        reg.unregister("custom")
        assert "custom" not in reg.list_providers()

    def test_register_preset(self):
        from companest.tools import ToolRegistry
        reg = ToolRegistry()
        reg.register_preset("trader", ["place_order", "get_markets", "memory_read"])
        assert "mcp__mem__memory_read" in reg.resolve_tool_names(["trader"])

    @pytest.mark.parametrize("deny,allowed", [
        ({"Bash"}, ["Read", "Write"]),
        ({"Bash", "Write"}, ["Read"]),
    ])
    def test_resolve_with_deny_filter(self, deny, allowed):
        from companest.tools import ToolRegistry
        result = ToolRegistry().resolve_tool_names(["Read", "Write", "Bash"], tools_deny=deny)
        for name in ("Read", "Write", "Bash"):
            assert (name in result) == (name in allowed)

    def test_default_deny_list(self):
        """Default PiConfig.tools_deny=[] falls back to DEFAULT_TOOLS_DENY, which bans Bash."""
        from companest.pi import PiConfig
        from companest.tools import DEFAULT_TOOLS_DENY
        assert PiConfig(id="test").tools_deny == []
        assert "Bash" in DEFAULT_TOOLS_DENY

    def test_deny_none_allows_all(self, tmp_path):
        from companest.pi import Pi, PiConfig

        (tmp_path / "teams" / "test" / "memory").mkdir(parents=True)
        config = PiConfig(id="pi1", tools=["Read", "Bash"], tools_deny=["none"])
        pi = Pi(config, MemoryManager(str(tmp_path)), team_id="test")
        assert pi._build_deny_set() == set()

    @pytest.mark.parametrize("deny_line,expected", [
        ("- tools_deny: Bash, Write\n", ["Bash", "Write"]),
        ("", []),
    ])
    def test_parse_tools_deny_from_markdown(self, deny_line, expected):
        from companest.team import _parse_pi_sections
        text = (
            "# Team: test\n"
            "#### Pi: agent1\n"
            "- model: claude-haiku-4-5-20251001\n"
            "- tools: Read, Write\n"
            f"{deny_line}"
            "- max_turns: 5\n"
        )
        pis = _parse_pi_sections(text)
        assert len(pis) == 1
        assert pis[0].tools_deny == expected


def test_exception_details():
    from companest.exceptions import PiError
    e = PiError("test error", details={"model": "claude"})
    assert "test error" in str(e)
    assert "claude" in str(e)


class TestServerEndpoints:
    """API endpoints exist on the FastAPI app and auth is enforced at startup."""

    @staticmethod
    def _server(debug):
        pytest.importorskip("fastapi")

        from companest.config import CompanestConfig
        from companest.jobs import JobManager
        from companest.server import CompanestAPIServer

        jm = JobManager.__new__(JobManager)
        jm.orchestrator = None
        jm._data_dir = None
        return CompanestAPIServer(CompanestConfig(debug=debug), jm)

    def test_endpoints_registered(self):
        app = self._server(debug=True).create_app()  # debug: no auth token required

        routes = [r.path for r in app.routes]
        for path in (
            "/api/teams",
            "/api/teams/{team_id}",
            "/api/teams/{team_id}/run",
            "/api/finance/summary",
            "/api/finance/approve/{approval_id}",
            "/api/finance/report",
            "/api/finance/circuit-breaker/reset",
            "/api/scheduler/status",
            "/api/scheduler/{task_name}/trigger",
            "/api/v2/status",
        ):
            assert path in routes

    def test_server_refuses_start_without_auth_in_production(self):
        """Server must refuse to start without COMPANEST_API_TOKEN when debug=False."""
        with pytest.raises(RuntimeError, match="COMPANEST_API_TOKEN is required"):
            self._server(debug=False).create_app()


class TestCompanyScoping:
    """Company-scoped routing helpers."""

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
