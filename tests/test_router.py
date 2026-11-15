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


    def test_no_default_returns_none(self):
        from companest.orchestration.router import TeamRouter
        router = TeamRouter(available_teams=["stock"], default_team=None)
        assert router.route("hello world") is None


class TestSmartRouterParsing:
    """SmartRouter._parse_response() with various LLM outputs, and keyword fallback."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.router = _smart_router(tmp_path, [("stock", "analyst"), ("engineering", "coder")])


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
        assert decision.mode == "default"
        assert decision.cascade is True
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
