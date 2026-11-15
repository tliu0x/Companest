import pytest

from companest.memory import MemoryManager

from design_helpers import _make_team, _run


def _smart_router(base, teams):
    from companest.agents.team import TeamRegistry
    from companest.orchestration.router import SmartRouter

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


class TestRouter:
    """Keyword-based team routing."""

    def setup_method(self):
        from companest.orchestration.router import TeamRouter
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
        from companest.orchestration.router import TeamRouter
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

    def test_routing_survives_audit_failure(self):
        class EventBus:
            async def emit(self, *args):
                raise RuntimeError("audit unavailable")

        self.router._event_bus = EventBus()
        self.router.add_binding(r"stock", "stock")
        decision = _run(self.router.route("Check this stock"))
        assert decision.teams[0].team_id == "stock"

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
