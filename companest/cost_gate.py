"""
Companest CostGate  Finance Department Approval System

Two operating modes:
1. **post_hoc** (default)  approve everything, reconcile with LiteLLM actuals,
   circuit breaker trips on anomalous velocity.
2. **approval**  original three-tier approval:
   a. < auto_approve_threshold  auto approve (silent)
   b. < escalation_threshold  auto approve + notify user
   c. >= escalation_threshold  escalate to user via Telegram, wait for approval

Priority multipliers scale thresholds (critical=bypass, high=3x, normal=1x, low=0.5x).
Per-team budgets with overflow pool. Rolling window replaces daily cutoff.
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from .memory import MemoryManager
from .exceptions import CostGateError

logger = logging.getLogger(__name__)

# Valid priority levels
VALID_PRIORITIES = {"critical", "high", "normal", "low"}

# Model pricing ($/1M tokens)  kept in sync with soul.md
# "langs": {lang_code: strength}  language affinity for cascade sorting
MODEL_PRICES = {
    "deepseek-chat":              {"input": 0.14,  "output": 0.28,  "langs": {"zh": 1.0, "en": 0.9}},
    "gpt-4o-mini":                {"input": 0.15,  "output": 0.60,  "langs": {"en": 1.0, "multi": 0.8}},
    "qwen3-coder":                {"input": 0.22,  "output": 1.00,  "langs": {"zh": 1.0, "en": 0.8}},
    "deepseek-reasoner":          {"input": 0.55,  "output": 2.19,  "langs": {"zh": 1.0, "en": 0.9}},
    "kimi-k2.5":                  {"input": 0.60,  "output": 3.00,  "langs": {"zh": 1.0, "en": 0.7}},
    "glm-4.7":                    {"input": 0.60,  "output": 2.20,  "langs": {"zh": 1.0, "en": 0.6}},
    "glm-5":                      {"input": 1.00,  "output": 3.20,  "langs": {"zh": 1.0, "en": 0.7}},
    "moonshot-v1-8k":             {"input": 1.00,  "output": 2.00,  "langs": {"zh": 1.0, "en": 0.7}},
    "claude-haiku-4-5":           {"input": 1.00,  "output": 5.00,  "langs": {"en": 1.0, "multi": 0.9}},
    "claude-haiku-4-5-20251001":  {"input": 1.00,  "output": 5.00,  "langs": {"en": 1.0, "multi": 0.9}},
    "gpt-4o":                     {"input": 2.50,  "output": 10.00, "langs": {"en": 1.0, "multi": 0.9}},
    "claude-sonnet-4-5":          {"input": 3.00,  "output": 15.00, "langs": {"en": 1.0, "multi": 0.9}},
    "claude-sonnet-4-5-20250929": {"input": 3.00,  "output": 15.00, "langs": {"en": 1.0, "multi": 0.9}},
    "claude-opus-4-6":            {"input": 5.00,  "output": 25.00, "langs": {"en": 1.0, "multi": 0.95}},
    "moonshot-v1-128k":           {"input": 6.00,  "output": 12.00, "langs": {"zh": 1.0, "en": 0.7}},
    "o3":                         {"input": 10.00, "output": 40.00, "langs": {"en": 1.0, "multi": 0.85}},
}


@dataclass
class SpendingVelocity:
    """A single spending event for velocity tracking."""
    timestamp: float   # time.monotonic()
    amount: float
    team_id: str


@dataclass
class CostEstimate:
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    target_team: str
    target_model: str
    suggested_downgrade: Optional[str] = None


@dataclass
class CostDecision:
    action: str  # "auto_approve" | "notify_approve" | "pending_approval" | "rejected"
    estimate: CostEstimate
    reason: str
    approval_id: Optional[str] = None
    priority: str = "normal"  # records what priority was used


class CircuitBreaker:
    """
    In-memory velocity tracker. Trips when spend within window_minutes
    exceeds threshold_pct of daily_limit. Auto-resets after cooldown_minutes.
    """

    def __init__(
        self,
        window_minutes: float = 5,
        threshold_pct: float = 30.0,
        cooldown_minutes: float = 15,
    ):
        self.window_minutes = window_minutes
        self.threshold_pct = threshold_pct
        self.cooldown_minutes = cooldown_minutes
        self._events: deque[SpendingVelocity] = deque()
        self._tripped_at: Optional[float] = None

    def record(self, amount: float, team_id: str) -> None:
        """Append spending event and prune old ones."""
        now = time.monotonic()
        self._events.append(SpendingVelocity(
            timestamp=now, amount=amount, team_id=team_id,
        ))
        self._prune(now)

    def _prune(self, now: float) -> None:
        """Remove events outside the window."""
        cutoff = now - self.window_minutes * 60
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def is_tripped(self, daily_limit: float) -> bool:
        """Check if breaker is tripped. Auto-resets after cooldown."""
        now = time.monotonic()

        # Auto-reset after cooldown
        if self._tripped_at is not None:
            elapsed = now - self._tripped_at
            if elapsed >= self.cooldown_minutes * 60:
                self._tripped_at = None
                logger.info("[CircuitBreaker] Auto-reset after cooldown")
                return False
            return True

        # Check velocity
        self._prune(now)
        window_spend = sum(e.amount for e in self._events)
        threshold = daily_limit * (self.threshold_pct / 100.0)
        if window_spend >= threshold:
            self._tripped_at = now
            logger.warning(
                f"[CircuitBreaker] TRIPPED: ${window_spend:.4f} in {self.window_minutes}min "
                f"exceeds {self.threshold_pct}% of ${daily_limit:.2f} limit"
            )
            return True

        return False

    def reset(self) -> None:
        """Manual reset."""
        self._tripped_at = None
        self._events.clear()
        logger.info("[CircuitBreaker] Manually reset")

    def get_status(self) -> dict:
        """Status dict for dashboards."""
        now = time.monotonic()
        self._prune(now)
        window_spend = sum(e.amount for e in self._events)
        tripped = self._tripped_at is not None
        cooldown_remaining = 0.0
        if tripped and self._tripped_at is not None:
            elapsed = now - self._tripped_at
            cooldown_remaining = max(0.0, self.cooldown_minutes * 60 - elapsed)

        return {
            "tripped": tripped,
            "window_spend": round(window_spend, 6),
            "window_minutes": self.window_minutes,
            "threshold_pct": self.threshold_pct,
            "cooldown_minutes": self.cooldown_minutes,
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "events_in_window": len(self._events),
        }


class CostGate:
    """
    Cost approval gate with two operating modes:

    - **post_hoc** (default): approve everything, reconcile later, circuit breaker
      trips on anomalous velocity.
    - **approval**: three-tier approval (cheap=silent, medium=notify, expensive=escalate).

    Per-team budgets with overflow pool. Priority multipliers scale thresholds.
    Rolling window replaces daily cutoff.
    """

    def __init__(
        self,
        memory: MemoryManager,
        notifier: Optional["UserNotifier"] = None,
        litellm_client: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        self.memory = memory
        self.notifier = notifier
        self.litellm_client = litellm_client
        self._event_bus = event_bus
        self._pending: Dict[str, asyncio.Future] = {}
        self._budget_lock = asyncio.Lock()
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._background_tasks: set = set()  # prevent GC of fire-and-forget tasks

    def _load_budget(self) -> Dict[str, Any]:
        budget = self.memory.read_team_memory("finance", "budget.json")
        if not budget:
            budget = {
                "auto_approve_threshold": 0.05,
                "escalation_threshold": 1.00,
                "daily_limit": 10.00,
                "monthly_limit": 200.00,
            }

        # Backward-compatible defaults for new fields
        budget.setdefault("mode", "post_hoc")
        budget.setdefault("rolling_window_hours", 24)
        budget.setdefault("circuit_breaker", {"window_minutes": 5, "threshold_pct": 30})
        budget.setdefault("team_budgets", {})
        budget.setdefault("overflow_pool", 0.0)
        budget.setdefault("priority_multipliers", {
            "critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5,
        })

        # Lazy-init circuit breaker from budget config
        if self._circuit_breaker is None:
            cb_cfg = budget["circuit_breaker"]
            self._circuit_breaker = CircuitBreaker(
                window_minutes=cb_cfg.get("window_minutes", 5),
                threshold_pct=cb_cfg.get("threshold_pct", 30),
                cooldown_minutes=cb_cfg.get("cooldown_minutes", 15),
            )

        return budget

    def _get_today_spending(self) -> float:
        """Get today's spend from local log (sync fallback)."""
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(
            entry.get("cost", 0)
            for entry in log
            if isinstance(entry, dict) and entry.get("date", "").startswith(today)
        )

    def _get_window_spending(self, hours: float) -> float:
        """Get spending within a rolling window of `hours`."""
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total = 0.0
        for entry in log:
            if not isinstance(entry, dict):
                continue
            date_str = entry.get("date", "")
            if not date_str:
                continue
            try:
                entry_time = datetime.fromisoformat(date_str)
                if entry_time >= cutoff:
                    total += entry.get("cost", 0)
            except (ValueError, TypeError):
                continue
        return total

    def _get_team_window_spending(self, team_id: str, hours: float) -> float:
        """Get per-team spending within a rolling window."""
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total = 0.0
        for entry in log:
            if not isinstance(entry, dict):
                continue
            if entry.get("team") != team_id:
                continue
            date_str = entry.get("date", "")
            if not date_str:
                continue
            try:
                entry_time = datetime.fromisoformat(date_str)
                if entry_time >= cutoff:
                    total += entry.get("cost", 0)
            except (ValueError, TypeError):
                continue
        return total

    def _get_company_window_spending(self, company_id: str, hours: float) -> float:
        """Get per-company spending within a rolling window."""
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total = 0.0
        for entry in log:
            if not isinstance(entry, dict):
                continue
            if entry.get("company") != company_id:
                continue
            date_str = entry.get("date", "")
            if not date_str:
                continue
            try:
                entry_time = datetime.fromisoformat(date_str)
                if entry_time >= cutoff:
                    total += entry.get("cost", 0)
            except (ValueError, TypeError):
                continue
        return total

    def _get_overflow_usage(self, budget: dict, hours: float) -> float:
        """Calculate how much overflow pool has been consumed.

        Overflow = sum of per-team overspend beyond their individual budgets.
        """
        team_budgets = budget.get("team_budgets", {})
        if not team_budgets:
            return 0.0

        overflow_used = 0.0
        for tid, tb in team_budgets.items():
            team_daily = tb.get("daily", 0)
            if team_daily <= 0:
                continue
            team_spent = self._get_team_window_spending(tid, hours)
            if team_spent > team_daily:
                overflow_used += team_spent - team_daily
        return overflow_used

    async def get_today_spending_async(self) -> float:
        """Get today's spend  prefer LiteLLM (accurate), fall back to local log."""
        if self.litellm_client:
            try:
                report = await self.litellm_client.get_total_spend()
                return float(report.get("total_spend", 0.0))
            except Exception as e:
                logger.warning(f"LiteLLM spend query failed, using local: {e}")
        return self._get_today_spending()

    def estimate_cost(self, task: str, target_model: str, target_team: str) -> CostEstimate:
        """Quick rule-based cost estimation (conservative  overestimates to be safe)."""
        from .cascade import get_downgrade

        # Conservative: ~2 chars per token (handles CJK, code, mixed)
        task_tokens = max(len(task) // 2, 10)
        prices = MODEL_PRICES.get(target_model)
        if not prices:
            # Unknown model: use expensive fallback to avoid budget overruns
            logger.warning(f"Unknown model '{target_model}', using conservative pricing")
            prices = {"input": 15.0, "output": 75.0}

        # System prompt: soul.md + user.md + team memory keys ~ 2000 tokens typical
        est_input = task_tokens + 2000
        est_output = min(task_tokens * 3, 8192)

        cost = (est_input * prices["input"] + est_output * prices["output"]) / 1_000_000

        return CostEstimate(
            estimated_input_tokens=est_input,
            estimated_output_tokens=est_output,
            estimated_cost_usd=round(cost, 4),
            target_team=target_team,
            target_model=target_model,
            suggested_downgrade=get_downgrade(target_model),
        )

    async def evaluate(
        self, task: str, target_team_id: str, target_model: str,
        priority: str = "normal",
        company_id: Optional[str] = None,
        company_budget_hourly: Optional[float] = None,
    ) -> CostDecision:
        """
        Evaluate a task and return approval decision.

        Modes:
        - **post_hoc**: default approve, circuit breaker can override.
        - **approval**: three-tier (auto/notify/escalate).

        Priority "critical" always bypasses all checks.

        Args:
            company_id: If set, check company-level hourly budget.
            company_budget_hourly: Company's hourly budget cap (from CompanyPreferences).
        """
        async with self._budget_lock:
            return await self._evaluate_locked(
                task, target_team_id, target_model, priority,
                company_id=company_id,
                company_budget_hourly=company_budget_hourly,
            )

    async def _evaluate_locked(
        self, task: str, target_team_id: str, target_model: str, priority: str,
        company_id: Optional[str] = None,
        company_budget_hourly: Optional[float] = None,
    ) -> CostDecision:
        """Internal evaluate  called under _budget_lock."""
        budget = self._load_budget()
        estimate = self.estimate_cost(task, target_model, target_team_id)
        cost = estimate.estimated_cost_usd

        # Validate priority
        if priority not in VALID_PRIORITIES:
            priority = "normal"

        # 1. Critical priority  always bypass
        if priority == "critical":
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Critical priority bypass", priority=priority,
            )

        # 1.5. Company-level hourly budget check
        if company_id and company_budget_hourly is not None and company_budget_hourly > 0:
            company_spent = self._get_company_window_spending(company_id, hours=1.0)
            if company_spent + cost > company_budget_hourly:
                return CostDecision(
                    action="rejected", estimate=estimate,
                    reason=(
                        f"Company '{company_id}' hourly budget exceeded "
                        f"(${company_spent:.2f}+${cost:.2f} > ${company_budget_hourly:.2f}/hr)"
                    ),
                    priority=priority,
                )

        # 2. Check circuit breaker
        if self._circuit_breaker and self._circuit_breaker.is_tripped(budget.get("daily_limit", 10.0)):
            # Emit event (fire-and-forget)
            if self._event_bus:
                try:
                    from .events import EventType
                    t = asyncio.create_task(self._event_bus.emit(
                        EventType.CIRCUIT_BREAKER_TRIPPED,
                        self._circuit_breaker.get_status(),
                    ))
                    self._background_tasks.add(t)
                    t.add_done_callback(self._background_tasks.discard)
                except Exception:
                    pass
            if self.notifier:
                approval_id = f"approval-{uuid4().hex[:8]}"
                window_spent = self._get_window_spending(budget.get("rolling_window_hours", 24))
                return await self._escalate(
                    approval_id, task, estimate, window_spent, budget,
                    reason="Circuit breaker tripped  anomalous spending velocity",
                    priority=priority,
                )
            return CostDecision(
                action="rejected", estimate=estimate,
                reason="Circuit breaker tripped  anomalous spending velocity",
                priority=priority,
            )

        # 3. Per-team budget check (if configured)
        team_budgets = budget.get("team_budgets", {})
        hours = budget.get("rolling_window_hours", 24)
        if target_team_id in team_budgets:
            team_daily = team_budgets[target_team_id].get("daily", 0)
            if team_daily > 0:
                team_spent = self._get_team_window_spending(target_team_id, hours)
                if team_spent + cost > team_daily:
                    # Try overflow pool
                    overflow_pool = budget.get("overflow_pool", 0.0)
                    overflow_used = self._get_overflow_usage(budget, hours)
                    overflow_remaining = max(0.0, overflow_pool - overflow_used)
                    overage = (team_spent + cost) - team_daily
                    if overage <= overflow_remaining:
                        logger.info(
                            f"[CostGate] Team {target_team_id} over budget, "
                            f"using ${overage:.4f} from overflow pool"
                        )
                    else:
                        # Over budget, no overflow left
                        if self.notifier:
                            approval_id = f"approval-{uuid4().hex[:8]}"
                            return await self._escalate(
                                approval_id, task, estimate, team_spent, budget,
                                reason=(
                                    f"Team '{target_team_id}' budget exceeded "
                                    f"(${team_spent:.2f}+${cost:.2f} > ${team_daily:.2f})"
                                ),
                                priority=priority,
                            )
                        return CostDecision(
                            action="rejected", estimate=estimate,
                            reason=(
                                f"Team '{target_team_id}' budget exceeded "
                                f"(${team_spent:.2f}+${cost:.2f} > ${team_daily:.2f})"
                            ),
                            priority=priority,
                        )

        mode = budget.get("mode", "post_hoc")

        # 4. Post-hoc mode (default)  auto_approve, reconcile later
        if mode == "post_hoc":
            # Check rolling window limit as a safety net
            window_spent = self._get_window_spending(hours)
            daily_limit = budget.get("daily_limit", 10.0)
            if window_spent + cost > daily_limit and priority != "high":
                if self.notifier:
                    approval_id = f"approval-{uuid4().hex[:8]}"
                    return await self._escalate(
                        approval_id, task, estimate, window_spent, budget,
                        reason=f"Rolling window limit exceeded (${window_spent:.2f} + ${cost:.2f} > ${daily_limit:.2f})",
                        priority=priority,
                    )
                return CostDecision(
                    action="rejected", estimate=estimate,
                    reason=f"Rolling window limit exceeded (${window_spent:.2f} + ${cost:.2f} > ${daily_limit:.2f})",
                    priority=priority,
                )
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Post-hoc mode: auto-approved",
                priority=priority,
            )

        # 5. Approval mode  three-tier with priority-adjusted thresholds
        multipliers = budget.get("priority_multipliers", {
            "critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5,
        })
        multiplier = multipliers.get(priority, 1.0)

        auto_threshold = budget.get("auto_approve_threshold", 0.05) * multiplier
        escalation_threshold = budget.get("escalation_threshold", 1.0) * multiplier

        # Check rolling window limit
        window_spent = self._get_window_spending(hours)
        daily_limit = budget.get("daily_limit", 10.0)
        if window_spent + cost > daily_limit:
            if self.notifier:
                approval_id = f"approval-{uuid4().hex[:8]}"
                return await self._escalate(
                    approval_id, task, estimate, window_spent, budget,
                    reason=f"Rolling window limit exceeded (spent ${window_spent:.2f} / ${daily_limit:.2f})",
                    priority=priority,
                )
            return CostDecision(
                action="rejected", estimate=estimate,
                reason=f"Rolling window limit exceeded (${window_spent:.2f} + ${cost:.2f} > ${daily_limit:.2f})",
                priority=priority,
            )

        # Tier 1: cheap  auto approve
        if cost < auto_threshold:
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Below auto-approve threshold",
                priority=priority,
            )

        # Tier 2: medium  approve + notify
        if cost < escalation_threshold:
            if self.notifier:
                t = asyncio.create_task(self.notifier.notify_cost(estimate, task))
                self._background_tasks.add(t)
                t.add_done_callback(self._background_tasks.discard)
            return CostDecision(
                action="notify_approve", estimate=estimate,
                reason="Medium cost, user notified",
                priority=priority,
            )

        # Tier 3: expensive  escalate
        if self.notifier:
            approval_id = f"approval-{uuid4().hex[:8]}"
            return await self._escalate(
                approval_id, task, estimate, window_spent, budget,
                reason="Above escalation threshold",
                priority=priority,
            )

        # No notifier configured  auto approve with warning
        logger.warning(f"No notifier configured, auto-approving ${cost:.4f} task")
        return CostDecision(
            action="auto_approve", estimate=estimate,
            reason="No notifier configured, auto-approved",
            priority=priority,
        )

    async def _escalate(
        self, approval_id: str, task: str, estimate: CostEstimate,
        today_spent: float, budget: dict, reason: str,
        priority: str = "normal",
    ) -> CostDecision:
        """Send approval request to user and wait for response."""
        future = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future

        await self.notifier.request_approval(
            approval_id=approval_id,
            task=task,
            estimate=estimate,
            today_spent=today_spent,
            daily_limit=budget.get("daily_limit", 10.0),
        )

        try:
            user_choice = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            self._pending.pop(approval_id, None)
            return CostDecision(
                action="rejected", estimate=estimate,
                reason="Approval timeout (5 minutes)", approval_id=approval_id,
            )
        finally:
            self._pending.pop(approval_id, None)

        if user_choice == "approve":
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="User approved", approval_id=approval_id,
                priority=priority,
            )
        elif user_choice == "downgrade":
            estimate.target_model = estimate.suggested_downgrade or "claude-haiku-4-5-20251001"
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="User approved (downgraded)", approval_id=approval_id,
                priority=priority,
            )
        else:
            return CostDecision(
                action="rejected", estimate=estimate,
                reason="User rejected", approval_id=approval_id,
                priority=priority,
            )

    def resolve_approval(self, approval_id: str, choice: str) -> bool:
        """Resolve a pending approval (called from API endpoint)."""
        future = self._pending.get(approval_id)
        if future and not future.done():
            future.set_result(choice)
            return True
        return False

    def record_spending(
        self, team_id: str, task: str, tokens: Dict[str, int], cost: float,
        actual_cost: Optional[float] = None,
        company_id: Optional[str] = None,
    ) -> None:
        """Record spending to finance memory. Feeds circuit breaker."""
        effective_cost = actual_cost if actual_cost is not None else cost
        entry = {
            "date": datetime.now(timezone.utc).isoformat(),
            "team": team_id,
            "task": task[:100],
            "tokens": tokens,
            "cost": round(effective_cost, 6),
            "estimated_cost": round(cost, 6),
            "reconciled": actual_cost is not None,
        }
        if company_id:
            entry["company"] = company_id
        self.memory.append_team_memory("finance", "spending-log.json", entry)

        # Feed circuit breaker
        if self._circuit_breaker:
            self._circuit_breaker.record(effective_cost, team_id)

    def get_daily_report(self, hours: float = 24) -> Dict[str, Any]:
        """Generate report dict with window spend, utilization, per-team breakdown, breaker status."""
        budget = self._load_budget()
        window_spent = self._get_window_spending(hours)
        daily_limit = budget.get("daily_limit", 10.0)

        # Per-team breakdown
        by_team: Dict[str, float] = {}
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if log and isinstance(log, list):
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            for entry in log:
                if not isinstance(entry, dict):
                    continue
                date_str = entry.get("date", "")
                if not date_str:
                    continue
                try:
                    entry_time = datetime.fromisoformat(date_str)
                    if entry_time >= cutoff:
                        tid = entry.get("team", "unknown")
                        by_team[tid] = by_team.get(tid, 0) + entry.get("cost", 0)
                except (ValueError, TypeError):
                    continue

        # Team budget utilization
        team_budgets = budget.get("team_budgets", {})
        team_utilization = {}
        for tid, tb in team_budgets.items():
            team_daily = tb.get("daily", 0)
            if team_daily > 0:
                team_spent = by_team.get(tid, 0)
                team_utilization[tid] = {
                    "spent": round(team_spent, 4),
                    "budget": team_daily,
                    "utilization_pct": round(team_spent / team_daily * 100, 1) if team_daily > 0 else 0,
                }

        report = {
            "window_hours": hours,
            "window_spend": round(window_spent, 4),
            "daily_limit": daily_limit,
            "utilization_pct": round(window_spent / daily_limit * 100, 1) if daily_limit > 0 else 0,
            "by_team": {k: round(v, 4) for k, v in by_team.items()},
            "team_utilization": team_utilization,
            "mode": budget.get("mode", "post_hoc"),
            "circuit_breaker": self._circuit_breaker.get_status() if self._circuit_breaker else None,
            "overflow_pool": budget.get("overflow_pool", 0.0),
            "overflow_used": round(self._get_overflow_usage(budget, hours), 4),
        }
        return report

    def get_spending_summary(self, days: int = 7) -> Dict[str, Any]:
        """Get spending summary for dashboard."""
        budget = self._load_budget()
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return {
                "total": 0, "by_team": {}, "entries": 0,
                "source": "litellm" if self.litellm_client else "local",
                "mode": budget.get("mode", "post_hoc"),
                "circuit_breaker": self._circuit_breaker.get_status() if self._circuit_breaker else None,
            }

        # Filter entries to the requested time window
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for e in log:
            if not isinstance(e, dict):
                continue
            date_str = e.get("date", "")
            if date_str:
                try:
                    if datetime.fromisoformat(date_str) >= cutoff:
                        filtered.append(e)
                except (ValueError, TypeError):
                    filtered.append(e)  # include entries with unparseable dates
            else:
                filtered.append(e)

        total = sum(e.get("cost", 0) for e in filtered)
        by_team: Dict[str, float] = {}
        for e in filtered:
            tid = e.get("team", "unknown")
            by_team[tid] = by_team.get(tid, 0) + e.get("cost", 0)

        hours = budget.get("rolling_window_hours", 24)
        window_spend = self._get_window_spending(hours)

        result = {
            "total": round(total, 4),
            "by_team": {k: round(v, 4) for k, v in by_team.items()},
            "entries": len(filtered),
            "days": days,
            "today": round(self._get_today_spending(), 4),
            "window_spend": round(window_spend, 4),
            "budget": budget,
            "source": "litellm" if self.litellm_client else "local",
            "mode": budget.get("mode", "post_hoc"),
            "circuit_breaker": self._circuit_breaker.get_status() if self._circuit_breaker else None,
        }
        return result


class UserNotifier:
    """
    Sends approval requests and notifications to the user.

    Supports two delivery modes:
    1. Callback function (for integration with MasterConnection or Telegram)
    2. Logging fallback (when no callback is configured)

    Usage:
        notifier = UserNotifier()
        # Later, when master connection is ready:
        notifier.send_fn = async_send_callback  # async (str) -> None
    """

    def __init__(self, send_fn: Optional[Callable] = None):
        self.send_fn = send_fn

    async def _send(self, msg: str, metadata: Optional[Dict] = None) -> None:
        """Send a message via callback or log."""
        if self.send_fn:
            try:
                result = self.send_fn(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"[UserNotifier] Failed to send message: {e}")
        else:
            logger.info(f"[UserNotifier] (no send_fn configured) {msg}")

    async def request_approval(
        self, approval_id: str, task: str, estimate: CostEstimate,
        today_spent: float, daily_limit: float,
    ) -> None:
        """Send approval request to user."""
        total_tokens = estimate.estimated_input_tokens + estimate.estimated_output_tokens
        msg = (
            f"*Companest Finance Approval Request*\n"
            f"\n"
            f"Task: {task[:80]}...\n"
            f"Team: {estimate.target_team} -> {estimate.target_model}\n"
            f"Estimate: ~{total_tokens:,} tokens = ${estimate.estimated_cost_usd:.2f}\n"
            f"Today's spending: ${today_spent:.2f} / ${daily_limit:.2f}\n"
            f"\n"
            f"Approval ID: {approval_id}\n"
            f"Reply: approve / downgrade / reject"
        )
        logger.info(f"[CostGate] Approval request: {approval_id} (${estimate.estimated_cost_usd:.2f})")
        await self._send(msg, {
            "type": "approval_request",
            "approval_id": approval_id,
            "cost": estimate.estimated_cost_usd,
            "team": estimate.target_team,
            "model": estimate.target_model,
        })

    async def notify_cost(self, estimate: CostEstimate, task: str) -> None:
        """Non-blocking cost notification."""
        msg = f"Companest: {estimate.target_team} spent ${estimate.estimated_cost_usd:.2f} -- {task[:50]}..."
        logger.info(f"[CostGate] {msg}")
        await self._send(msg, {"type": "cost_notification"})

    async def notify_overspend(
        self, estimate: CostEstimate, actual_cost: float, task: str
    ) -> None:
        """Overspend alert."""
        pct = (actual_cost / max(estimate.estimated_cost_usd, 0.0001) - 1) * 100
        msg = (
            f"*Overspend Alert*\n"
            f"Task: {task[:50]}...\n"
            f"Estimated: ${estimate.estimated_cost_usd:.2f} -> Actual: ${actual_cost:.2f} (+{pct:.0f}%)"
        )
        logger.info(f"[CostGate] {msg}")
        await self._send(msg, {"type": "overspend_alert", "actual_cost": actual_cost})

    async def notify_daily_report(self, report: Dict[str, Any]) -> None:
        """Formatted daily spend report."""
        window = report.get("window_hours", 24)
        spend = report.get("window_spend", 0)
        limit = report.get("daily_limit", 0)
        pct = report.get("utilization_pct", 0)

        by_team = report.get("by_team", {})
        team_lines = "\n".join(f"  {tid}: ${amt:.2f}" for tid, amt in sorted(by_team.items()))

        breaker = report.get("circuit_breaker", {})
        breaker_status = "TRIPPED" if breaker and breaker.get("tripped") else "OK"

        msg = (
            f"*Companest Daily Finance Report*\n"
            f"\n"
            f"Window: {window}h\n"
            f"Spend: ${spend:.2f} / ${limit:.2f} ({pct:.0f}%)\n"
            f"Circuit breaker: {breaker_status}\n"
            f"\nBy team:\n{team_lines or '  (no spending)'}"
        )
        logger.info(f"[CostGate] Daily report: ${spend:.2f}/{limit:.2f}")
        await self._send(msg, {"type": "daily_report"})

    async def notify_circuit_breaker(self, status: Dict[str, Any]) -> None:
        """Alert when circuit breaker trips."""
        msg = (
            f"*Circuit Breaker Alert*\n"
            f"\n"
            f"Status: {'TRIPPED' if status.get('tripped') else 'OK'}\n"
            f"Window spend: ${status.get('window_spend', 0):.4f}\n"
            f"Cooldown remaining: {status.get('cooldown_remaining_seconds', 0):.0f}s"
        )
        logger.warning(f"[CostGate] {msg}")
        await self._send(msg, {"type": "circuit_breaker_alert", **status})
