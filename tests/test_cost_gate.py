import time

import pytest

from companest.memory import MemoryManager

from design_helpers import _run


class TestCostGate:
    """Cost approval tiers, per-team budgets, overflow pool, priorities, reports."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from companest.orchestration.cost_gate import CostGate

        (tmp_path / "teams" / "finance" / "memory").mkdir(parents=True)
        self.mm = MemoryManager(str(tmp_path))
        self.cg = CostGate(self.mm)

    def _budget(self, **budget):
        self.mm.write_team_memory("finance", "budget.json", budget)

    def _notifying_gate(self):
        from companest.orchestration.cost_gate import CostGate, UserNotifier
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
        from companest.orchestration.cost_gate import MODEL_PRICES
        from companest.agents.cascade import get_downgrade
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

    @pytest.mark.parametrize("cost,normal,low", [
        (0.0049, "auto_approve", "auto_approve"),
        (0.005, "auto_approve", "notify_approve"),
        (0.007, "auto_approve", "notify_approve"),
        (0.01, "notify_approve", "notify_approve"),
    ])
    def test_low_priority_approval_boundary(self, monkeypatch, cost, normal, low):
        from companest.orchestration.cost_gate import CostEstimate

        self._budget(
            auto_approve_threshold=0.01, escalation_threshold=1.0,
            daily_limit=100.0, mode="approval",
            priority_multipliers={"normal": 1.0, "low": 0.5},
        )
        estimate = CostEstimate(100, 100, cost, "stock", "test-model")
        monkeypatch.setattr(self.cg, "estimate_cost", lambda *args: estimate)
        for priority, expected in (("normal", normal), ("low", low)):
            decision = _run(self.cg.evaluate("task", "stock", "test-model", priority=priority))
            assert decision.action == expected
            assert decision.priority == priority

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
        from companest.orchestration.cost_gate import CircuitBreaker
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
