import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

TASK_TIMEOUT_SECONDS = 300


@dataclass
class ScheduledTask:
    name: str
    func: Callable[[], Coroutine]
    interval_seconds: int
    enabled: bool = True
    run_on_start: bool = False
    last_run: Optional[datetime] = None
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    _executing: bool = field(default=False, repr=False)


class Scheduler:
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
        if name in self._running_tasks:
            self._running_tasks[name].cancel()
            del self._running_tasks[name]
        if name in self._tasks:
            del self._tasks[name]
            return True
        return False

    def remove_by_scope(self, scope_type: str, scope_id: str) -> List[str]:
        removed = []
        for name in list(self._tasks):
            task = self._tasks[name]
            if task.scope_type == scope_type and task.scope_id == scope_id:
                self.remove(name)
                removed.append(name)
        return removed

    async def start(self) -> None:
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
        self._started = False
        for atask in self._running_tasks.values():
            atask.cancel()
        await asyncio.gather(
            *self._running_tasks.values(), return_exceptions=True
        )
        self._running_tasks.clear()
        logger.info("[Scheduler] Stopped")

    async def _run_loop(self, task: ScheduledTask) -> None:
        try:
            if task.run_on_start:
                await self._execute(task)

            while True:
                await asyncio.sleep(task.interval_seconds)
                await self._execute(task)
        except asyncio.CancelledError:
            logger.debug(f"[Scheduler] Task '{task.name}' cancelled")
        except Exception as e:
            logger.error(f"[Scheduler] Task '{task.name}' loop died: {e}")

    async def _execute(self, task: ScheduledTask) -> None:
        if task._executing:
            logger.warning(f"[Scheduler] Skipping '{task.name}': still running from previous interval")
            return
        task._executing = True
        try:
            logger.debug(f"[Scheduler] Running '{task.name}'")
            await asyncio.wait_for(task.func(), timeout=TASK_TIMEOUT_SECONDS)
            task.last_run = datetime.now(timezone.utc)
            task.run_count += 1
        except asyncio.TimeoutError:
            task.error_count += 1
            task.last_error = f"Timed out after {TASK_TIMEOUT_SECONDS}s"
            logger.error(
                f"[Scheduler] Task '{task.name}' timed out after {TASK_TIMEOUT_SECONDS}s"
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
        task = self._tasks.get(name)
        if not task:
            return False
        await self._execute(task)
        return True
