import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from ..exceptions import SchedulerError

logger = logging.getLogger(__name__)

ExecutionCallback = Callable[["ScheduledJob"], Coroutine[Any, Any, str]]

NotificationCallback = Callable[[str, str, str, str], Coroutine[Any, Any, None]]

_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',
    task TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_type TEXT NOT NULL,
    trigger_args TEXT NOT NULL DEFAULT '{}',
    team_id TEXT,
    mode TEXT NOT NULL DEFAULT 'default',
    fire_count INTEGER NOT NULL DEFAULT 0,
    last_fired TEXT,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""


@dataclass
class ScheduledJob:
    id: str
    user_id: str
    chat_id: str
    channel: str
    task: str
    description: str
    trigger_type: str  # "cron", "interval", "date"
    trigger_args: Dict[str, Any]
    team_id: Optional[str] = None
    mode: str = "default"
    fire_count: int = 0
    last_fired: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    active: bool = True
    company_id: Optional[str] = None
    workspace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "channel": self.channel,
            "task": self.task,
            "description": self.description,
            "trigger_type": self.trigger_type,
            "trigger_args": self.trigger_args,
            "team_id": self.team_id,
            "mode": self.mode,
            "fire_count": self.fire_count,
            "last_fired": self.last_fired,
            "created_at": self.created_at,
            "active": self.active,
            "company_id": self.company_id,
            "workspace_id": self.workspace_id,
        }


class UserScheduler:
    """User-facing scheduler wrapping APScheduler 3.x."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._db_path = data_dir / "user_schedules.db"
        self._db: Optional[Any] = None
        self._scheduler = None
        self._execution_callback: Optional[ExecutionCallback] = None
        self._notification_callback: Optional[NotificationCallback] = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        # _fire_job is called synchronously by APScheduler and needs this loop.
        self._loop = asyncio.get_running_loop()

        self._data_dir.mkdir(parents=True, exist_ok=True)

        import aiosqlite
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(_METADATA_SCHEMA)
        columns = await (await self._db.execute("PRAGMA table_info(scheduled_jobs)")).fetchall()
        for column in ("company_id", "workspace_id"):
            if column not in {row[1] for row in columns}:
                await self._db.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {column} TEXT")
        await self._db.commit()

        # APScheduler jobs live in memory only; _restore_jobs rebuilds them from SQLite (no pickling).
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self._scheduler.start()
        self._started = True

        await self._restore_jobs()

        logger.info(f"UserScheduler started (db={self._db_path})")

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

        if self._db:
            await self._db.close()
            self._db = None

        self._started = False
        logger.info("UserScheduler stopped")

    def set_execution_callback(self, callback: ExecutionCallback) -> None:
        self._execution_callback = callback

    def set_notification_callback(self, callback: NotificationCallback) -> None:
        self._notification_callback = callback

    async def add_job(
        self,
        user_id: str,
        chat_id: str,
        channel: str,
        task: str,
        description: str,
        trigger_type: str,
        trigger_args: Dict[str, Any],
        team_id: Optional[str] = None,
        mode: str = "default",
        company_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ScheduledJob:
        if not self._started:
            raise SchedulerError("UserScheduler not started")

        if trigger_type not in ("cron", "interval", "date"):
            raise SchedulerError(f"Invalid trigger_type: {trigger_type}")

        job_id = str(uuid.uuid4())[:8]

        job = ScheduledJob(
            id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            channel=channel,
            task=task,
            description=description,
            trigger_type=trigger_type,
            trigger_args=trigger_args,
            team_id=team_id,
            mode=mode,
            company_id=company_id,
            workspace_id=workspace_id,
        )

        await self._save_job_metadata(job)

        self._scheduler.add_job(
            self._fire_job,
            trigger=trigger_type,
            id=job_id,
            args=[job_id],
            replace_existing=True,
            **trigger_args,
        )

        logger.info(
            f"Scheduled job {job_id}: trigger={trigger_type}, "
            f"user={user_id}, task={task[:50]}"
        )
        return job

    async def cancel_job(self, job_id: str, user_id: Optional[str] = None) -> bool:
        """Cancel a scheduled job. Supports partial ID match. If user_id is provided, enforces ownership."""
        if not self._started:
            raise SchedulerError("UserScheduler not started")

        full_id = await self._resolve_job_id(job_id, user_id)
        if not full_id:
            return False

        if user_id:
            row = await self._get_job_row(full_id)
            if row and row["user_id"] != user_id:
                return False

        try:
            self._scheduler.remove_job(full_id)
        except Exception:
            pass  # May already be gone (date trigger fired)

        await self._db.execute(
            "UPDATE scheduled_jobs SET active = 0 WHERE id = ?",
            (full_id,),
        )
        await self._db.commit()

        logger.info(f"Cancelled job {full_id}")
        return True

    async def list_jobs(self, user_id: Optional[str] = None) -> List[ScheduledJob]:
        """List active scheduled jobs, optionally filtered by user."""
        if not self._db:
            return []

        if user_id:
            cursor = await self._db.execute(
                "SELECT * FROM scheduled_jobs WHERE active = 1 AND user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM scheduled_jobs WHERE active = 1 ORDER BY created_at DESC",
            )

        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        return [self._row_to_job(dict(zip(columns, row))) for row in rows]

    def get_status(self) -> Dict[str, Any]:
        status = {
            "started": self._started,
            "db_path": str(self._db_path),
        }
        if self._scheduler:
            jobs = self._scheduler.get_jobs()
            status["active_jobs"] = len(jobs)
            status["next_run"] = str(jobs[0].next_run_time) if jobs else None
        return status

    def _fire_job(self, job_id: str) -> None:
        def _on_task_done(task):
            exc = task.exception() if not task.cancelled() else None
            if exc:
                logger.error(f"Scheduled job {job_id} raised: {exc}")

        try:
            loop = self._loop
            if loop.is_running():
                t = loop.create_task(self._fire_job_async(job_id))
                t.add_done_callback(_on_task_done)
            else:
                loop.run_until_complete(self._fire_job_async(job_id))
        except (RuntimeError, AttributeError):
            logger.error(f"No event loop for job {job_id}")

    async def _fire_job_async(self, job_id: str) -> None:
        row = await self._get_job_row(job_id)
        if not row:
            logger.warning(f"Job {job_id} metadata not found, skipping")
            return

        job = self._row_to_job(row)
        logger.info(f"Firing job {job_id}: {job.task[:60]}")

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE scheduled_jobs SET fire_count = fire_count + 1, last_fired = ? WHERE id = ?",
            (now, job_id),
        )
        await self._db.commit()

        result = None
        if self._execution_callback:
            try:
                result = await self._execution_callback(job)
            except Exception as e:
                logger.error(f"Job {job_id} execution failed: {e}")
                result = f"Scheduled task failed: {e}"

        if result and self._notification_callback:
            try:
                header = f"[Scheduled] {job.description or job.task[:50]}\n\n"
                await self._notification_callback(
                    job.chat_id, job.channel, job.user_id,
                    header + result,
                )
            except Exception as e:
                logger.error(f"Job {job_id} notification failed: {e}")

        if job.trigger_type == "date":
            await self._db.execute(
                "UPDATE scheduled_jobs SET active = 0 WHERE id = ?",
                (job_id,),
            )
            await self._db.commit()
            logger.info(f"One-shot job {job_id} completed and deactivated")

    async def _restore_jobs(self) -> None:
        cursor = await self._db.execute(
            "SELECT * FROM scheduled_jobs WHERE active = 1",
        )
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]

        restored = 0
        for row in rows:
            job = self._row_to_job(dict(zip(columns, row)))
            try:
                self._scheduler.add_job(
                    self._fire_job,
                    trigger=job.trigger_type,
                    id=job.id,
                    args=[job.id],
                    replace_existing=True,
                    **job.trigger_args,
                )
                restored += 1
            except Exception as e:
                # Date triggers in the past fail to schedule; deactivate them.
                logger.debug(f"Could not restore job {job.id}: {e}")
                await self._db.execute(
                    "UPDATE scheduled_jobs SET active = 0 WHERE id = ?",
                    (job.id,),
                )

        await self._db.commit()
        if restored:
            logger.info(f"Restored {restored} persisted job(s)")

    async def _save_job_metadata(self, job: ScheduledJob) -> None:
        await self._db.execute(
            """INSERT INTO scheduled_jobs
               (id, user_id, chat_id, channel, task, description,
                trigger_type, trigger_args, team_id, mode,
                fire_count, last_fired, created_at, active, company_id, workspace_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id, job.user_id, job.chat_id, job.channel,
                job.task, job.description,
                job.trigger_type, json.dumps(job.trigger_args),
                job.team_id, job.mode,
                job.fire_count, job.last_fired, job.created_at,
                1 if job.active else 0, job.company_id, job.workspace_id,
            ),
        )
        await self._db.commit()

    async def _get_job_row(self, job_id: str) -> Optional[Dict]:
        cursor = await self._db.execute(
            "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))

    async def _resolve_job_id(
        self, partial_id: str, user_id: Optional[str] = None,
    ) -> Optional[str]:
        if user_id:
            cursor = await self._db.execute(
                "SELECT id FROM scheduled_jobs WHERE id LIKE ? AND user_id = ? AND active = 1",
                (f"{partial_id}%", user_id),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id FROM scheduled_jobs WHERE id LIKE ? AND active = 1",
                (f"{partial_id}%",),
            )
        row = await cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _row_to_job(row: Dict) -> ScheduledJob:
        return ScheduledJob(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            channel=row["channel"],
            task=row["task"],
            description=row["description"],
            trigger_type=row["trigger_type"],
            trigger_args=json.loads(row["trigger_args"]),
            team_id=row["team_id"],
            mode=row["mode"],
            fire_count=row["fire_count"],
            last_fired=row["last_fired"],
            created_at=row["created_at"],
            active=bool(row["active"]),
            company_id=row.get("company_id"),
            workspace_id=row.get("workspace_id"),
        )
