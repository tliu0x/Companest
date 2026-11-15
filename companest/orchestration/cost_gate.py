"""Companest CostGate  Finance Department Approval System"""

import asyncio
import threading
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from ..utils import run_sync, synchronized
from ..memory import MemoryManager

logger = logging.getLogger(__name__)

VALID_PRIORITIES = {"critical", "high", "normal", "low"}

# $/1M tokens; "langs" is the language affinity used to order cascade chains
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
    timestamp: float   # time.monotonic()
    amount: float
    team_id: str


@dataclass
class CostEstimate:
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    team_id: str
    model: str
    suggested_downgrade: Optional[str] = None


@dataclass
class CostDecision:
    action: str  # "auto_approve" | "notify_approve" | "rejected"
    estimate: CostEstimate
    reason: str
    approval_id: Optional[str] = None
    priority: str = "normal"


class CircuitBreaker:
    def __init__(
        self,
        window_minutes: float = 5,
        threshold_pct: float = 30.0,
        cooldown_minutes: float = 15,
    ):
        self._io_lock = threading.RLock()
        self.window_minutes = window_minutes
        self.threshold_pct = threshold_pct
        self.cooldown_minutes = cooldown_minutes
        self._events: deque[SpendingVelocity] = deque()
        self._tripped_at: Optional[float] = None

    @synchronized
    def record(self, amount: float, team_id: str) -> None:
        now = time.monotonic()
        self._events.append(SpendingVelocity(
            timestamp=now, amount=amount, team_id=team_id,
        ))
        self._prune(now)

    @synchronized
    def _prune(self, now: float) -> None:
        cutoff = now - self.window_minutes * 60
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    @synchronized
    def is_tripped(self, daily_limit: float) -> bool:
        now = time.monotonic()
        if self._tripped_at is not None:
            if now - self._tripped_at >= self.cooldown_minutes * 60:
                self._tripped_at = None
                logger.info("[CircuitBreaker] Auto-reset after cooldown")
                return False
            return True

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

    @synchronized
    def reset(self) -> None:
        self._tripped_at = None
        self._events.clear()
        logger.info("[CircuitBreaker] Manually reset")

    @synchronized
    def get_status(self) -> dict:
        now = time.monotonic()
        self._prune(now)
        window_spend = sum(e.amount for e in self._events)
        tripped = self._tripped_at is not None
        cooldown_remaining = 0.0
        if tripped:
            cooldown_remaining = max(0.0, self.cooldown_minutes * 60 - (now - self._tripped_at))

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
    def __init__(
        self,
        memory: MemoryManager,
        notifier: Optional["UserNotifier"] = None,
        event_bus: Optional[Any] = None,
    ):
        self._io_lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self.memory = memory
        self.notifier = notifier
        self._event_bus = event_bus
        self._pending: Dict[str, asyncio.Future] = {}
        self._pending_metadata: Dict[str, Dict[str, Any]] = {}
        self._budget_lock = asyncio.Lock()
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._background_tasks: set = set()  # prevent GC of fire-and-forget tasks

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)

    @synchronized
    def _load_budget(self) -> Dict[str, Any]:
        budget = self.memory.read_team_memory("finance", "budget.json")
        if not budget:
            budget = {
                "auto_approve_threshold": 0.05,
                "escalation_threshold": 1.00,
                "daily_limit": 10.00,
                "monthly_limit": 200.00,
            }

        budget.setdefault("mode", "post_hoc")
        budget.setdefault("rolling_window_hours", 24)
        budget.setdefault("circuit_breaker", {"window_minutes": 5, "threshold_pct": 30})
        budget.setdefault("team_budgets", {})
        budget.setdefault("overflow_pool", 0.0)
        budget.setdefault("priority_multipliers", {
            "critical": 999, "high": 3.0, "normal": 1.0, "low": 0.5,
        })

        if self._circuit_breaker is None:
            cb_cfg = budget["circuit_breaker"]
            self._circuit_breaker = CircuitBreaker(
                window_minutes=cb_cfg.get("window_minutes", 5),
                threshold_pct=cb_cfg.get("threshold_pct", 30),
                cooldown_minutes=cb_cfg.get("cooldown_minutes", 15),
            )

        return budget

    def _get_today_spending(self) -> float:
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(
            entry.get("cost", 0)
            for entry in log
            if isinstance(entry, dict) and entry.get("date", "").startswith(today)
        )

    def _get_window_spending(self, hours: float, field: Optional[str] = None, value: Any = None) -> float:
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return 0.0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total = 0.0
        for entry in log:
            if not isinstance(entry, dict) or (field and entry.get(field) != value):
                continue
            try:
                if datetime.fromisoformat(entry.get("date", "")) >= cutoff:
                    total += entry.get("cost", 0)
            except (ValueError, TypeError):
                continue
        return total

    def _get_team_window_spending(self, team_id: str, hours: float) -> float:
        return self._get_window_spending(hours, "team", team_id)

    def _get_company_window_spending(self, company_id: str, hours: float) -> float:
        return self._get_window_spending(hours, "company", company_id)

    def _get_overflow_usage(self, budget: dict, hours: float) -> float:
        overflow_used = 0.0
        for tid, tb in budget["team_budgets"].items():
            team_daily = tb.get("daily", 0)
            if team_daily <= 0:
                continue
            team_spent = self._get_team_window_spending(tid, hours)
            if team_spent > team_daily:
                overflow_used += team_spent - team_daily
        return overflow_used

    def estimate_cost(self, task: str, model: str, team_id: str) -> CostEstimate:
        from ..agents.cascade import get_downgrade

        # Deliberately overestimates: ~2 chars per token, ~2000 tokens of system prompt
        task_tokens = max(len(task) // 2, 10)
        prices = MODEL_PRICES.get(model)
        if not prices:
            logger.warning(f"Unknown model '{model}', using conservative pricing")
            prices = {"input": 15.0, "output": 75.0}

        est_input = task_tokens + 2000
        est_output = min(task_tokens * 3, 8192)
        cost = (est_input * prices["input"] + est_output * prices["output"]) / 1_000_000
        return CostEstimate(
            estimated_input_tokens=est_input,
            estimated_output_tokens=est_output,
            estimated_cost_usd=round(cost, 4),
            team_id=team_id,
            model=model,
            suggested_downgrade=get_downgrade(model),
        )

    async def evaluate(
        self, task: str, team_id: str, model: str,
        priority: str = "normal",
        company_id: Optional[str] = None,
        company_budget_hourly: Optional[float] = None,
    ) -> CostDecision:
        async with self._budget_lock:
            return await self._evaluate_locked(
                task, team_id, model, priority, company_id, company_budget_hourly,
            )

    async def _evaluate_locked(
        self, task: str, team_id: str, model: str, priority: str,
        company_id: Optional[str], company_budget_hourly: Optional[float],
    ) -> CostDecision:
        budget = await run_sync(self._load_budget)
        estimate = self.estimate_cost(task, model, team_id)
        cost = estimate.estimated_cost_usd
        daily_limit = budget.get("daily_limit", 10.0)
        hours = budget["rolling_window_hours"]

        if priority not in VALID_PRIORITIES:
            priority = "normal"

        def rejected(reason: str) -> CostDecision:
            return CostDecision(action="rejected", estimate=estimate, reason=reason, priority=priority)

        if priority == "critical":
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Critical priority bypass", priority=priority,
            )

        if company_id and company_budget_hourly is not None and company_budget_hourly > 0:
            company_spent = await run_sync(self._get_company_window_spending, company_id, hours=1.0)
            if company_spent + cost > company_budget_hourly:
                return rejected(
                    f"Company '{company_id}' hourly budget exceeded "
                    f"(${company_spent:.2f}+${cost:.2f} > ${company_budget_hourly:.2f}/hr)"
                )

        if self._circuit_breaker.is_tripped(daily_limit):
            if self._event_bus:
                from ..events import EventType
                self._spawn(self._event_bus.emit(
                    EventType.CIRCUIT_BREAKER_TRIPPED,
                    self._circuit_breaker.get_status(),
                ))
            reason = "Circuit breaker tripped  anomalous spending velocity"
            if self.notifier:
                return await self._escalate(
                    task, estimate, await run_sync(self._get_window_spending, hours), daily_limit, reason, priority,
                )
            return rejected(reason)

        team_daily = budget["team_budgets"].get(team_id, {}).get("daily", 0)
        if team_daily > 0:
            team_spent = await run_sync(self._get_team_window_spending, team_id, hours)
            if team_spent + cost > team_daily:
                overflow_remaining = max(
                    0.0, budget["overflow_pool"] - await run_sync(self._get_overflow_usage, budget, hours),
                )
                overage = (team_spent + cost) - team_daily
                if overage <= overflow_remaining:
                    logger.info(
                        f"[CostGate] Team {team_id} over budget, "
                        f"using ${overage:.4f} from overflow pool"
                    )
                else:
                    reason = (
                        f"Team '{team_id}' budget exceeded "
                        f"(${team_spent:.2f}+${cost:.2f} > ${team_daily:.2f})"
                    )
                    if self.notifier:
                        return await self._escalate(task, estimate, team_spent, daily_limit, reason, priority)
                    return rejected(reason)

        post_hoc = budget["mode"] == "post_hoc"
        window_spent = await run_sync(self._get_window_spending, hours)
        # In post-hoc mode "high" priority is exempt from the rolling-window limit
        if window_spent + cost > daily_limit and not (post_hoc and priority == "high"):
            reason = f"Rolling window limit exceeded (${window_spent:.2f} + ${cost:.2f} > ${daily_limit:.2f})"
            if self.notifier:
                return await self._escalate(
                    task, estimate, window_spent, daily_limit,
                    reason if post_hoc
                    else f"Rolling window limit exceeded (spent ${window_spent:.2f} / ${daily_limit:.2f})",
                    priority,
                )
            return rejected(reason)

        if post_hoc:
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Post-hoc mode: auto-approved",
                priority=priority,
            )

        multiplier = budget["priority_multipliers"].get(priority, 1.0)
        if cost < budget.get("auto_approve_threshold", 0.05) * multiplier:
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="Below auto-approve threshold",
                priority=priority,
            )

        if cost < budget.get("escalation_threshold", 1.0) * multiplier:
            if self.notifier:
                self._spawn(self.notifier.notify_cost(estimate, task))
            return CostDecision(
                action="notify_approve", estimate=estimate,
                reason="Medium cost, user notified",
                priority=priority,
            )

        if self.notifier:
            return await self._escalate(
                task, estimate, window_spent, daily_limit, "Above escalation threshold", priority,
            )

        logger.warning(f"No notifier configured, auto-approving ${cost:.4f} task")
        return CostDecision(
            action="auto_approve", estimate=estimate,
            reason="No notifier configured, auto-approved",
            priority=priority,
        )

    async def _escalate(
        self, task: str, estimate: CostEstimate,
        today_spent: float, daily_limit: float, reason: str, priority: str,
    ) -> CostDecision:
        approval_id = f"approval-{uuid4().hex[:8]}"
        future = asyncio.get_running_loop().create_future()
        with self._pending_lock:
            self._pending[approval_id] = future
            self._pending_metadata[approval_id] = {
                "approval_id": approval_id,
                "task": task,
                "reason": reason,
                "priority": priority,
                "estimated_cost_usd": round(estimate.estimated_cost_usd, 4),
                "estimated_input_tokens": estimate.estimated_input_tokens,
                "estimated_output_tokens": estimate.estimated_output_tokens,
                "team_id": estimate.team_id,
                "model": estimate.model,
                "suggested_downgrade": estimate.suggested_downgrade,
                "today_spent": round(today_spent, 4),
                "daily_limit": daily_limit,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        await self.notifier.request_approval(
            approval_id=approval_id,
            task=task,
            estimate=estimate,
            today_spent=today_spent,
            daily_limit=daily_limit,
        )

        try:
            user_choice = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return CostDecision(
                action="rejected", estimate=estimate,
                reason="Approval timeout (5 minutes)", approval_id=approval_id,
            )
        finally:
            with self._pending_lock:
                self._pending.pop(approval_id, None)
                self._pending_metadata.pop(approval_id, None)

        if user_choice == "approve":
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="User approved", approval_id=approval_id,
                priority=priority,
            )
        if user_choice == "downgrade" and estimate.suggested_downgrade:
            estimate.model = estimate.suggested_downgrade
            return CostDecision(
                action="auto_approve", estimate=estimate,
                reason="User approved (downgraded)", approval_id=approval_id,
                priority=priority,
            )
        return CostDecision(
            action="rejected", estimate=estimate,
            reason="User rejected", approval_id=approval_id,
            priority=priority,
        )

    def resolve_approval(self, approval_id: str, choice: str) -> bool:
        future = self._pending.get(approval_id)
        if future and not future.done():
            future.set_result(choice)
            return True
        return False

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        with self._pending_lock:
            pending = [
                dict(metadata) for approval_id, metadata in self._pending_metadata.items()
                if not self._pending[approval_id].done()
            ]
        pending.sort(key=lambda item: item["created_at"], reverse=True)
        return pending

    def record_spending(
        self, team_id: str, task: str, tokens: Dict[str, int], cost: float,
        actual_cost: Optional[float] = None,
        company_id: Optional[str] = None,
    ) -> None:
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

        if self._circuit_breaker:
            self._circuit_breaker.record(effective_cost, team_id)

    def get_daily_report(self, hours: float = 24) -> Dict[str, Any]:
        budget = self._load_budget()
        daily_limit = budget.get("daily_limit", 10.0)

        window_spent = 0.0
        by_team: Dict[str, float] = {}
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if log and isinstance(log, list):
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            for entry in log:
                if not isinstance(entry, dict):
                    continue
                try:
                    if datetime.fromisoformat(entry.get("date", "")) >= cutoff:
                        cost = entry.get("cost", 0)
                        tid = entry.get("team", "unknown")
                        window_spent += cost
                        by_team[tid] = by_team.get(tid, 0) + cost
                except (ValueError, TypeError):
                    continue

        team_utilization = {}
        for tid, tb in budget["team_budgets"].items():
            team_daily = tb.get("daily", 0)
            if team_daily > 0:
                team_spent = by_team.get(tid, 0)
                team_utilization[tid] = {
                    "spent": round(team_spent, 4),
                    "budget": team_daily,
                    "utilization_pct": round(team_spent / team_daily * 100, 1),
                }

        return {
            "window_hours": hours,
            "window_spend": round(window_spent, 4),
            "daily_limit": daily_limit,
            "utilization_pct": round(window_spent / daily_limit * 100, 1) if daily_limit > 0 else 0,
            "by_team": {k: round(v, 4) for k, v in by_team.items()},
            "team_utilization": team_utilization,
            "mode": budget["mode"],
            "circuit_breaker": self._circuit_breaker.get_status(),
            "overflow_pool": budget["overflow_pool"],
            "overflow_used": round(self._get_overflow_usage(budget, hours), 4),
            "pending_approvals_count": len(self.get_pending_approvals()),
        }

    def get_spending_summary(self, days: int = 7) -> Dict[str, Any]:
        budget = self._load_budget()
        pending = self.get_pending_approvals()
        status = {
            "source": "local",
            "mode": budget["mode"],
            "circuit_breaker": self._circuit_breaker.get_status(),
            "pending_approvals": pending,
            "pending_approvals_count": len(pending),
        }
        log = self.memory.read_team_memory("finance", "spending-log.json")
        if not log or not isinstance(log, list):
            return {"total": 0, "by_team": {}, "entries": 0, **status}

        # Entries whose date is missing or unparseable are counted, not dropped
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for e in log:
            if not isinstance(e, dict):
                continue
            try:
                if datetime.fromisoformat(e.get("date", "")) < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            filtered.append(e)

        by_team: Dict[str, float] = {}
        for e in filtered:
            tid = e.get("team", "unknown")
            by_team[tid] = by_team.get(tid, 0) + e.get("cost", 0)

        return {
            "total": round(sum(e.get("cost", 0) for e in filtered), 4),
            "by_team": {k: round(v, 4) for k, v in by_team.items()},
            "entries": len(filtered),
            "days": days,
            "today": round(self._get_today_spending(), 4),
            "window_spend": round(self._get_window_spending(budget["rolling_window_hours"]), 4),
            "budget": budget,
            **status,
        }


class UserNotifier:
    def __init__(self, send_fn: Optional[Callable] = None):
        self.send_fn = send_fn

    async def _send(self, msg: str) -> None:
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
        total_tokens = estimate.estimated_input_tokens + estimate.estimated_output_tokens
        choices = "approve / reject"
        if estimate.suggested_downgrade:
            choices = f"approve / downgrade ({estimate.suggested_downgrade}) / reject"
        msg = (
            f"*Companest Finance Approval Request*\n"
            f"\n"
            f"Task: {task[:80]}...\n"
            f"Team: {estimate.team_id} -> {estimate.model}\n"
            f"Estimate: ~{total_tokens:,} tokens = ${estimate.estimated_cost_usd:.2f}\n"
            f"Today's spending: ${today_spent:.2f} / ${daily_limit:.2f}\n"
            f"\n"
            f"Approval ID: {approval_id}\n"
            f"Reply: {choices}"
        )
        logger.info(f"[CostGate] Approval request: {approval_id} (${estimate.estimated_cost_usd:.2f})")
        await self._send(msg)

    async def notify_cost(self, estimate: CostEstimate, task: str) -> None:
        msg = f"Companest: {estimate.team_id} spent ${estimate.estimated_cost_usd:.2f} -- {task[:50]}..."
        logger.info(f"[CostGate] {msg}")
        await self._send(msg)

    async def notify_daily_report(self, report: Dict[str, Any]) -> None:
        spend = report["window_spend"]
        limit = report["daily_limit"]
        team_lines = "\n".join(f"  {tid}: ${amt:.2f}" for tid, amt in sorted(report["by_team"].items()))
        breaker_status = "TRIPPED" if report["circuit_breaker"]["tripped"] else "OK"

        msg = (
            f"*Companest Daily Finance Report*\n"
            f"\n"
            f"Window: {report['window_hours']}h\n"
            f"Spend: ${spend:.2f} / ${limit:.2f} ({report['utilization_pct']:.0f}%)\n"
            f"Circuit breaker: {breaker_status}\n"
            f"\nBy team:\n{team_lines or '  (no spending)'}"
        )
        logger.info(f"[CostGate] Daily report: ${spend:.2f}/{limit:.2f}")
        await self._send(msg)
