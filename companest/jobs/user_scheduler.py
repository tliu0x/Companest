import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from ..exceptions import SchedulerError

logger = logging.getLogger(__name__)

ExecutionCallback = Callable[["Schedule"], Coroutine[Any, Any, str]]

NotificationCallback = Callable[[str, str, str, str], Coroutine[Any, Any, None]]

_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    schedule_id TEXT PRIMARY KEY,
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
class Schedule:
    schedule_id: str
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
            "schedule_id": self.schedule_id,
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

        self._data_dir.mkdir(parents=True, exist_ok=True)

        import aiosqlite
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(_METADATA_SCHEMA)
        columns = await (await self._db.execute("PRAGMA table_info(scheduled_jobs)")).fetchall()
        if "id" in {row[1] for row in columns}:
            await self._db.execute("ALTER TABLE scheduled_jobs RENAME COLUMN id TO schedule_id")
        for column in ("company_id", "workspace_id"):
            if column not in {row[1] for row in columns}:
                await self._db.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {column} TEXT")
        await self._db.commit()

        # APScheduler jobs live in memory only; _restore_schedules rebuilds them from SQLite (no pickling).
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self._scheduler.start()
        self._started = True

        await self._restore_schedules()

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

    async def add_schedule(
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
    ) -> Schedule:
        if not self._started:
            raise SchedulerError("UserScheduler not started")

        if trigger_type not in ("cron", "interval", "date"):
            raise SchedulerError(f"Invalid trigger_type: {trigger_type}")

        schedule_id = str(uuid.uuid4())

        schedule = Schedule(
            schedule_id=schedule_id,
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

        await self._save_schedule_metadata(schedule)

        self._scheduler.add_job(
            self._fire_schedule,
            trigger=trigger_type,
            id=schedule_id,
            args=[schedule_id],
            replace_existing=True,
            **trigger_args,
        )

        logger.info(
            f"Scheduled schedule {schedule_id}: trigger={trigger_type}, "
            f"user={user_id}, task={task[:50]}"
        )
        return schedule

    async def cancel_schedule(self, schedule_id: str, user_id: Optional[str] = None) -> bool:
        """Cancel a schedule by its exact schedule_id. If user_id is provided, enforces ownership."""
        if not self._started:
            raise SchedulerError("UserScheduler not started")

        if not await self._resolve_schedule_id(schedule_id, user_id):
            return False

        if user_id:
            row = await self._get_schedule_row(schedule_id)
            if row and row["user_id"] != user_id:
                return False

        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:
            pass  # May already be gone (date trigger fired)

        await self._db.execute(
            "UPDATE scheduled_jobs SET active = 0 WHERE schedule_id = ?",
            (schedule_id,),
        )
        await self._db.commit()

        logger.info(f"Cancelled schedule {schedule_id}")
        return True

    async def list_schedules(self, user_id: Optional[str] = None) -> List[Schedule]:
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
        return [self._row_to_schedule(dict(zip(columns, row))) for row in rows]

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

    async def _fire_schedule(self, schedule_id: str) -> None:
        # APScheduler awaits coroutine jobs on its owning loop. Do not detach
        # work: max_instances/coalescing must cover the complete remote task.
        await self._fire_schedule_async(schedule_id)

    async def _fire_schedule_async(self, schedule_id: str) -> None:
        row = await self._get_schedule_row(schedule_id)
        if not row or not row["active"]:
            logger.info(f"Job {schedule_id} is missing or inactive, skipping")
            return

        schedule = self._row_to_schedule(row)
        logger.info(f"Firing schedule {schedule_id}: {schedule.task[:60]}")

        # Claim one-shot schedules before dispatch: restart must not replay
        # an occurrence whose remote effects may already exist.
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            "UPDATE scheduled_jobs SET fire_count = fire_count + 1, last_fired = ?, "
            "active = CASE WHEN trigger_type = 'date' THEN 0 ELSE active END WHERE schedule_id = ? AND active = 1",
            (now, schedule_id),
        )
        await self._db.commit()
        if cursor.rowcount != 1:
            return

        result = None
        if self._execution_callback:
            try:
                result = await self._execution_callback(schedule)
            except Exception as e:
                logger.error(f"Job {schedule_id} execution failed: {e}")
                result = f"Scheduled task failed: {e}"

        if result and self._notification_callback:
            try:
                header = f"[Scheduled] {schedule.description or schedule.task[:50]}\n\n"
                await self._notification_callback(
                    schedule.chat_id, schedule.channel, schedule.user_id,
                    header + result,
                )
            except Exception as e:
                logger.error(f"Job {schedule_id} notification failed: {e}")

    async def _restore_schedules(self) -> None:
        cursor = await self._db.execute(
            "SELECT * FROM scheduled_jobs WHERE active = 1",
        )
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]

        restored = 0
        for row in rows:
            schedule = self._row_to_schedule(dict(zip(columns, row)))
            try:
                self._scheduler.add_job(
                    self._fire_schedule,
                    trigger=schedule.trigger_type,
                    id=schedule.schedule_id,
                    args=[schedule.schedule_id],
                    replace_existing=True,
                    **schedule.trigger_args,
                )
                restored += 1
            except Exception as e:
                # Date triggers in the past fail to schedule; deactivate them.
                logger.debug(f"Could not restore schedule {schedule.schedule_id}: {e}")
                await self._db.execute(
                    "UPDATE scheduled_jobs SET active = 0 WHERE schedule_id = ?",
                    (schedule.schedule_id,),
                )

        await self._db.commit()
        if restored:
            logger.info(f"Restored {restored} persisted schedule(s)")

    async def _save_schedule_metadata(self, schedule: Schedule) -> None:
        await self._db.execute(
            """INSERT INTO scheduled_jobs
               (schedule_id, user_id, chat_id, channel, task, description,
                trigger_type, trigger_args, team_id, mode,
                fire_count, last_fired, created_at, active, company_id, workspace_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule.schedule_id, schedule.user_id, schedule.chat_id, schedule.channel,
                schedule.task, schedule.description,
                schedule.trigger_type, json.dumps(schedule.trigger_args),
                schedule.team_id, schedule.mode,
                schedule.fire_count, schedule.last_fired, schedule.created_at,
                1 if schedule.active else 0, schedule.company_id, schedule.workspace_id,
            ),
        )
        await self._db.commit()

    async def _get_schedule_row(self, schedule_id: str) -> Optional[Dict]:
        cursor = await self._db.execute(
            "SELECT * FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))

    async def _resolve_schedule_id(
        self, schedule_id: str, user_id: Optional[str] = None,
    ) -> Optional[str]:
        if user_id:
            cursor = await self._db.execute(
                "SELECT schedule_id FROM scheduled_jobs WHERE schedule_id = ? AND user_id = ? AND active = 1",
                (schedule_id, user_id),
            )
        else:
            cursor = await self._db.execute(
                "SELECT schedule_id FROM scheduled_jobs WHERE schedule_id = ? AND active = 1",
                (schedule_id,),
            )
        row = await cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _row_to_schedule(row: Dict) -> Schedule:
        return Schedule(
            schedule_id=row["schedule_id"],
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
