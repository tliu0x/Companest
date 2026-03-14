"""
Companest Scheduler

Lightweight periodic task runner for background jobs:
- Memory backup to S3 (every 4 hours)
- Idle team eviction (every 5 minutes)
- Spending report (daily)
- Custom scheduled tasks

Uses asyncio  no external scheduler dependency.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from .exceptions import SchedulerError

logger = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    """A periodic task definition."""
    name: str
    func: Callable[[], Coroutine]
    interval_seconds: int
    enabled: bool = True
    run_on_start: bool = False
    last_run: Optional[datetime] = None
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    scope_type: Optional[str] = None  # "company" | "system"
    scope_id: Optional[str] = None    # company_id
    timeout: Optional[int] = None     # max seconds per execution (default 300)
    _executing: bool = field(default=False, repr=False)


class Scheduler:
    """
    Async periodic task scheduler.

    Usage:
        scheduler = Scheduler()
        scheduler.add("backup", archiver.backup_snapshot, interval=14400)
        scheduler.add("evict", registry.evict_idle, interval=300)
        await scheduler.start()
        # ... later ...
        await scheduler.stop()
    """

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._started = False

    def add(
        self,
        name: str,
        func: Callable[[], Coroutine],
        interval: int,
        enabled: bool = True,
        run_on_start: bool = False,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> None:
        """Register a periodic task."""
        if name in self._tasks:
            logger.warning(f"[Scheduler] Overwriting existing task: {name}")
        self._tasks[name] = ScheduledTask(
            name=name,
            func=func,
            interval_seconds=interval,
            enabled=enabled,
            run_on_start=run_on_start,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(
            f"[Scheduler] Registered '{name}' "
            f"(every {interval}s, {'enabled' if enabled else 'disabled'})"
        )

    def remove(self, name: str) -> bool:
        """Remove a scheduled task."""
        if name in self._running_tasks:
            self._running_tasks[name].cancel()
            del self._running_tasks[name]
        if name in self._tasks:
            del self._tasks[name]
            return True
        return False

    def remove_by_scope(self, scope_type: str, scope_id: str) -> List[str]:
        """Remove all tasks matching the given scope."""
        removed = []
        for name in list(self._tasks):
            task = self._tasks[name]
            if task.scope_type == scope_type and task.scope_id == scope_id:
                self.remove(name)
                removed.append(name)
        return removed

    def enable(self, name: str) -> bool:
        """Enable a disabled task."""
        if name in self._tasks:
            self._tasks[name].enabled = True
            if self._started and name not in self._running_tasks:
                self._running_tasks[name] = asyncio.create_task(
                    self._run_loop(self._tasks[name])
                )
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a task (stops its loop)."""
        if name in self._tasks:
            self._tasks[name].enabled = False
            if name in self._running_tasks:
                self._running_tasks[name].cancel()
                del self._running_tasks[name]
            return True
        return False

    async def start(self) -> None:
        """Start all enabled scheduled tasks."""
        if self._started:
            return
        self._started = True

        for name, task in self._tasks.items():
            if task.enabled:
                self._running_tasks[name] = asyncio.create_task(
                    self._run_loop(task)
                )

        logger.info(
            f"[Scheduler] Started with {len(self._running_tasks)} task(s)"
        )

    async def stop(self) -> None:
        """Stop all running tasks."""
        self._started = False
        for name, atask in self._running_tasks.items():
            atask.cancel()
        # Wait for all tasks to finish cancellation
        if self._running_tasks:
            await asyncio.gather(
                *self._running_tasks.values(), return_exceptions=True
            )
        self._running_tasks.clear()
        logger.info("[Scheduler] Stopped")

    async def _run_loop(self, task: ScheduledTask) -> None:
        """Main loop for a single scheduled task."""
        try:
            # Optionally run immediately on start
            if task.run_on_start:
                await self._execute(task)

            while True:
                await asyncio.sleep(task.interval_seconds)
                if not task.enabled:
                    break
                await self._execute(task)
        except asyncio.CancelledError:
            logger.debug(f"[Scheduler] Task '{task.name}' cancelled")
        except Exception as e:
            logger.error(f"[Scheduler] Task '{task.name}' loop died: {e}")

    async def _execute(self, task: ScheduledTask) -> None:
        """Execute a single task with error handling, timeout, and overlap prevention."""
        if task._executing:
            logger.warning(f"[Scheduler] Skipping '{task.name}': still running from previous interval")
            return
        task._executing = True
        effective_timeout = task.timeout or 300
        try:
            logger.debug(f"[Scheduler] Running '{task.name}'")
            await asyncio.wait_for(task.func(), timeout=effective_timeout)
            task.last_run = datetime.now(timezone.utc)
            task.run_count += 1
        except asyncio.TimeoutError:
            task.error_count += 1
            task.last_error = f"Timed out after {effective_timeout}s"
            logger.error(
                f"[Scheduler] Task '{task.name}' timed out after {effective_timeout}s"
            )
        except Exception as e:
            task.error_count += 1
            task.last_error = str(e)
            logger.error(
                f"[Scheduler] Task '{task.name}' failed "
                f"(attempt #{task.run_count + task.error_count}): {e}"
            )
        finally:
            task._executing = False

    def get_status(self) -> Dict[str, Any]:
        """Get status of all scheduled tasks."""
        return {
            "started": self._started,
            "tasks": {
                name: {
                    "enabled": t.enabled,
                    "interval_seconds": t.interval_seconds,
                    "last_run": t.last_run.isoformat() if t.last_run else None,
                    "run_count": t.run_count,
                    "error_count": t.error_count,
                    "last_error": t.last_error,
                    "running": name in self._running_tasks,
                }
                for name, t in self._tasks.items()
            },
        }

    async def run_now(self, name: str) -> bool:
        """Manually trigger a task immediately."""
        task = self._tasks.get(name)
        if not task:
            return False
        await self._execute(task)
        return True
