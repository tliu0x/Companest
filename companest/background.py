"""
Companest Background Manager

Manages scheduled background tasks: team eviction, daily reports,
enrichment cycles, on-demand info refresh, and user-scheduled job execution.
Extracted from CompanestOrchestrator to keep orchestrator slim.

Usage:
    bg = BackgroundManager(
        run_team_fn=orchestrator.run_team,
        run_auto_fn=orchestrator.run_auto,
        team_registry=orchestrator.team_registry,
        cost_gate=orchestrator.cost_gate,
        events=orchestrator.events,
        scheduler=orchestrator.scheduler,
        user_scheduler=orchestrator.user_scheduler,
        enrichment_cycles=orchestrator._enrichment_cycles,
        info_refresh_team=orchestrator._info_refresh_team,
    )
    bg.setup_schedules()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, Optional

from .events import EventBus, EventType

if TYPE_CHECKING:
    from .user_scheduler import ScheduledJob

logger = logging.getLogger(__name__)


class BackgroundManager:
    """
    Manages all scheduled background tasks for the orchestrator.

    Receives dependencies via constructor injection rather than
    accessing orchestrator attributes directly.
    """

    def __init__(
        self,
        run_team_fn: Callable,
        run_auto_fn: Callable,
        team_registry,
        cost_gate,
        events: EventBus,
        scheduler,
        user_scheduler,
        enrichment_cycles: Dict[str, dict],
        info_refresh_team: str,
    ):
        self._run_team = run_team_fn
        self._run_auto = run_auto_fn
        self._team_registry = team_registry
        self._cost_gate = cost_gate
        self._events = events
        self._scheduler = scheduler
        self._user_scheduler = user_scheduler
        self._enrichment_cycles = enrichment_cycles
        self._info_refresh_team = info_refresh_team
        self._notification_callback = None

    def setup_schedules(self) -> None:
        """Register all recurring background tasks with the scheduler."""
        # Idle team eviction (every 5 min)
        self._scheduler.add(
            "team_eviction",
            self.evict_idle_teams,
            interval=300,
        )

        # Daily spend report
        self._scheduler.add(
            "daily_spend_report",
            self.send_daily_report,
            interval=86400,
            run_on_start=False,
        )

        # Enrichment cycles (research, info-collection, custom)
        all_teams = set(self._team_registry.list_teams())
        for team_id, cycle in self._enrichment_cycles.items():
            if team_id in all_teams:
                safe_name = team_id.replace("-", "_") + "_cycle"
                self._scheduler.add(
                    safe_name,
                    lambda tid=team_id, p=cycle["prompt"]: self.run_enrichment_cycle(tid, p),
                    interval=cycle["interval"],
                    run_on_start=False,
                )

    async def evict_idle_teams(self) -> None:
        """Scheduled task: evict idle business teams."""
        evicted = self._team_registry.evict_idle()
        if evicted:
            logger.info(f"Evicted idle teams: {evicted}")
            for tid in evicted:
                await self._events.emit(EventType.TEAM_EVICTED, {"team_id": tid})

    async def send_daily_report(self) -> None:
        """Scheduled task: generate and send daily spend report."""
        try:
            report = self._cost_gate.get_daily_report(hours=24)
            if self._cost_gate.notifier:
                await self._cost_gate.notifier.notify_daily_report(report)
            logger.info(f"[Background] Daily report sent: ${report.get('window_spend', 0):.2f}")
        except Exception as e:
            logger.error(f"[Background] Daily report failed: {e}")

    async def run_enrichment_cycle(self, team_id: str, prompt: str) -> None:
        """Scheduled task: run an enrichment cycle for a team."""
        if team_id not in self._team_registry.list_teams():
            return

        try:
            await self._run_team(
                prompt,
                team_id,
                skip_cost_check=True,
                mode="cascade",
            )
            logger.info(f"[{team_id}] Enrichment cycle completed successfully")
        except Exception as e:
            logger.error(f"[{team_id}] Enrichment cycle failed: {e}")

    async def refresh_info_for_task(self, task: str) -> None:
        """On-demand: ask info team to do a quick targeted refresh."""
        team_id = self._info_refresh_team
        if team_id not in self._team_registry.list_teams():
            return

        prompt = (
            f"Quick targeted refresh. The user is asking about:\n\n"
            f"{task[:500]}\n\n"
            "Do a focused brave_search for this topic. "
            "Check if any of the watchlist sources have relevant new info. "
            "Update feed.json with any fresh findings. Be fast  max 2-3 searches."
        )

        try:
            await self._run_team(
                prompt,
                team_id,
                skip_cost_check=True,
                mode="cascade",
            )
            logger.info(f"[{team_id}] On-demand refresh completed")
        except Exception as e:
            logger.warning(f"[{team_id}] On-demand refresh failed: {e}")

    async def execute_scheduled_job(self, job: ScheduledJob) -> str:
        """
        Execution callback for UserScheduler  runs the scheduled task
        through the orchestrator pipeline.
        """
        user_context = {
            "user_id": job.user_id,
            "chat_id": job.chat_id,
            "channel": job.channel,
        }

        if job.team_id:
            return await self._run_team(
                task=job.task,
                team_id=job.team_id,
                mode=job.mode,
                skip_cost_check=True,
                user_context=user_context,
            )
        else:
            result, _ = await self._run_auto(
                task=job.task,
                user_context=user_context,
            )
            return result

    def set_notification_callback(self, callback) -> None:
        """Set the notification callback for user scheduler."""
        self._notification_callback = callback
        if self._user_scheduler is None:
            return
        self._user_scheduler.set_notification_callback(callback)

    def set_execution_callback(self, callback) -> None:
        """Set the execution callback for user scheduler."""
        if self._user_scheduler is None:
            return
        self._user_scheduler.set_execution_callback(callback)
