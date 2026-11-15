import uuid
import asyncio
import logging
import json
from typing import Dict, List, Optional, Any, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from ..exceptions import JobError

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: Dict[JobStatus, frozenset] = {
    JobStatus.PENDING: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.QUEUED: frozenset({JobStatus.DISPATCHED, JobStatus.CANCELLED}),
    JobStatus.DISPATCHED: frozenset({JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.WAITING_APPROVAL}),
    JobStatus.WAITING_APPROVAL: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


@dataclass
class Job:
    id: str
    task: str
    status: JobStatus = JobStatus.PENDING
    context: Dict[str, Any] = field(default_factory=dict)
    subtasks: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    approval_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    submitted_by: str = "api"
    company_id: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status.value,
            "context": self.context,
            "subtasks": self.subtasks,
            "result": self.result,
            "error": self.error,
            "approval_reason": self.approval_reason,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "submitted_by": self.submitted_by,
            "company_id": self.company_id,
            "duration_ms": self.duration_ms,
        }


class JobManager:
    MAX_IN_MEMORY_JOBS = 500

    def __init__(self, orchestrator=None, data_dir: Optional[Path] = None):
        self.orchestrator = orchestrator
        self._data_dir = data_dir or Path(".companest")
        self._jobs: Dict[str, Job] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._db = None

    def _transition(self, job: Job, new_status: JobStatus) -> None:
        old = job.status
        allowed = VALID_TRANSITIONS[old]
        if new_status not in allowed:
            logger.error(
                f"[JobManager] Invalid transition for job {job.id}: "
                f"{old.value}  {new_status.value} (allowed: {[s.value for s in allowed]})"
            )
            raise JobError(
                f"Invalid job state transition: {old.value}  {new_status.value}"
            )
        job.status = new_status
        logger.info(f"[JobManager] Job {job.id}: {old.value}  {new_status.value}")

    async def start(self, num_workers: int = 2) -> None:
        if self._running:
            return

        self._running = True

        await self._init_db()
        await self._load_jobs()

        for job in self._jobs.values():
            if job.status in (JobStatus.RUNNING, JobStatus.DISPATCHED):
                logger.warning(
                    f"Recovering interrupted job {job.id} "
                    f"({job.status.value} -> queued)"
                )
                job.status = JobStatus.QUEUED  # bypass _transition (not a normal flow)
                job.started_at = None
                await self._persist_job(job)

        self._queue = asyncio.Queue()
        for job in self._jobs.values():
            if job.status == JobStatus.PENDING:
                self._transition(job, JobStatus.QUEUED)
            if job.status == JobStatus.QUEUED:
                self._queue.put_nowait(job.id)

        for i in range(num_workers):
            self._workers.append(asyncio.create_task(self._worker(f"worker-{i}")))
        logger.info(f"JobManager started with {num_workers} workers")

    async def stop(self) -> None:
        self._running = False

        for worker in self._workers:
            worker.cancel()

        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass

        self._workers.clear()

        if self._db:
            await self._db.close()
            self._db = None

        logger.info("JobManager stopped")

    async def submit(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        submitted_by: str = "api",
        company_id: Optional[str] = None,
    ) -> str:
        ctx = dict(context or {})
        effective_company_id = company_id or ctx.get("company_id")
        if company_id and ctx.get("company_id") and company_id != ctx.get("company_id"):
            raise JobError(
                f"company_id mismatch: param={company_id}, context={ctx.get('company_id')}"
            )
        if effective_company_id and "company_id" not in ctx:
            ctx["company_id"] = effective_company_id
        job = Job(
            id=str(uuid.uuid4()),
            task=task,
            context=ctx,
            submitted_by=submitted_by,
            company_id=effective_company_id,
        )

        self._jobs[job.id] = job
        await self._persist_job(job)

        self._transition(job, JobStatus.QUEUED)
        await self._queue.put(job.id)

        logger.info(f"Job {job.id} submitted by {submitted_by}: {task[:80]}...")
        return job.id

    async def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def _matching(self, status: Optional[JobStatus], company_id: Optional[str]) -> List[Job]:
        return [
            j for j in self._jobs.values()
            if (not status or j.status == status)
            and (not company_id or j.company_id == company_id)
        ]

    async def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        company_id: Optional[str] = None,
    ) -> List[Job]:
        jobs = self._matching(status, company_id)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[offset:offset + limit]

    async def count_jobs(
        self,
        status: Optional[JobStatus] = None,
        company_id: Optional[str] = None,
    ) -> int:
        return len(self._matching(status, company_id))

    def get_company_activity_snapshot(
        self,
        company_ids: Optional[Iterable[str]] = None,
        recent_hours: float = 24.0,
    ) -> Dict[str, Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
        snapshot: Dict[str, Dict[str, Any]] = {
            cid: {"total_jobs": 0, "recent_job_count": 0, "last_activity_timestamp": None}
            for cid in company_ids or ()
        }
        latest: Dict[str, datetime] = {}
        for job in self._jobs.values():
            company_id = job.company_id
            if not company_id or (company_ids is not None and company_id not in snapshot):
                continue
            entry = snapshot.setdefault(
                company_id, {"total_jobs": 0, "recent_job_count": 0, "last_activity_timestamp": None},
            )
            entry["total_jobs"] += 1
            if job.created_at >= cutoff:
                entry["recent_job_count"] += 1
            activity = job.completed_at or job.started_at or job.created_at
            if company_id not in latest or activity > latest[company_id]:
                latest[company_id] = activity
                entry["last_activity_timestamp"] = activity.isoformat()
        return snapshot

    async def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            raise JobError(f"Job not found: {job_id}")

        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            return False

        self._transition(job, JobStatus.CANCELLED)
        job.completed_at = datetime.now(timezone.utc)
        await self._persist_job(job)

        logger.info(f"Job {job_id} cancelled")
        return True

    async def _worker(self, name: str) -> None:
        try:
            while self._running:
                try:
                    job_id = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                job = self._jobs.get(job_id)
                if not job or job.status != JobStatus.QUEUED:
                    continue

                logger.info(f"[{name}] Processing job {job_id}")
                try:
                    await self._execute_job(job)
                except asyncio.CancelledError:
                    if job.status == JobStatus.RUNNING:
                        job.status = JobStatus.FAILED
                        job.error = "Interrupted by shutdown"
                        job.completed_at = datetime.now(timezone.utc)
                        await self._persist_job(job)
                        logger.warning(f"[{name}] Job {job_id} interrupted by shutdown")
                    raise

        except asyncio.CancelledError:
            return

    async def _execute_job(self, job: Job) -> None:
        self._transition(job, JobStatus.DISPATCHED)
        job.started_at = datetime.now(timezone.utc)
        await self._persist_job(job)
        if job.status == JobStatus.CANCELLED:
            return

        if not self.orchestrator:
            self._transition(job, JobStatus.FAILED)
            job.error = "No orchestrator configured"
            job.completed_at = datetime.now(timezone.utc)
            await self._persist_job(job)
            return

        try:
            self._transition(job, JobStatus.RUNNING)
            await self._persist_job(job)
            if job.status == JobStatus.CANCELLED:
                return

            exec_context = dict(job.context or {})
            if job.company_id and "company_id" not in exec_context:
                exec_context["company_id"] = job.company_id

            team_id = exec_context.get("team_id")
            if team_id:
                result_text = await self.orchestrator.run_team(
                    task=job.task,
                    team_id=team_id,
                    user_context=exec_context,
                )
            else:
                result_text, _ = await self.orchestrator.run_auto(
                    task=job.task,
                    user_context=exec_context,
                )

            if job.status == JobStatus.CANCELLED:
                return
            self._transition(job, JobStatus.COMPLETED)
            job.result = result_text
            job.completed_at = datetime.now(timezone.utc)

        except Exception as e:
            if job.status == JobStatus.CANCELLED:
                return
            self._transition(job, JobStatus.FAILED)
            job.error = str(e)
            job.completed_at = datetime.now(timezone.utc)
            logger.error(f"Job {job.id} failed: {e}")

        await self._persist_job(job)
        self._evict_completed_jobs()

    async def _init_db(self) -> None:
        try:
            import aiosqlite
        except ImportError:
            logger.warning(
                "aiosqlite not installed, job persistence disabled. "
                "Install with: pip install aiosqlite"
            )
            return

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._data_dir / "jobs.db"))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                context TEXT,
                subtasks TEXT,
                result TEXT,
                error TEXT,
                approval_reason TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                submitted_by TEXT,
                company_id TEXT
            )
        """)
        await self._db.commit()

        # Databases created before these columns existed still need them added.
        for column in ("approval_reason", "company_id"):
            try:
                await self._db.execute(f"SELECT {column} FROM jobs LIMIT 1")
            except Exception:
                await self._db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                await self._db.commit()

    async def _persist_job(self, job: Job) -> None:
        if not self._db:
            return

        try:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO jobs
                (id, task, status, context, subtasks, result, error,
                 approval_reason, created_at, started_at, completed_at, submitted_by,
                 company_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.task,
                    job.status.value,
                    json.dumps(job.context),
                    json.dumps(job.subtasks),
                    job.result,
                    job.error,
                    job.approval_reason,
                    job.created_at.isoformat(),
                    job.started_at.isoformat() if job.started_at else None,
                    job.completed_at.isoformat() if job.completed_at else None,
                    job.submitted_by,
                    job.company_id,
                ),
            )
            await self._db.commit()
        except Exception as e:
            logger.error(f"Failed to persist job {job.id}: {e}")

    async def _load_jobs(self) -> None:
        if not self._db:
            return

        try:
            async with self._db.execute(
                "SELECT id, task, status, context, subtasks, result, error, "
                "approval_reason, created_at, started_at, completed_at, submitted_by, "
                "company_id "
                "FROM jobs ORDER BY created_at DESC LIMIT 1000"
            ) as cursor:
                rows = await cursor.fetchall()

            for row in rows:
                job = Job(
                    id=row[0],
                    task=row[1],
                    status=JobStatus(row[2]),
                    context=json.loads(row[3]) if row[3] else {},
                    subtasks=json.loads(row[4]) if row[4] else [],
                    result=row[5],
                    error=row[6],
                    approval_reason=row[7],
                    created_at=datetime.fromisoformat(row[8]),
                    started_at=datetime.fromisoformat(row[9]) if row[9] else None,
                    completed_at=datetime.fromisoformat(row[10]) if row[10] else None,
                    submitted_by=row[11] or "api",
                    company_id=row[12],
                )
                self._jobs[job.id] = job

            logger.info(f"Loaded {len(rows)} jobs from database")
        except Exception as e:
            logger.error(f"Failed to load jobs: {e}")

    def _evict_completed_jobs(self) -> None:
        if len(self._jobs) <= self.MAX_IN_MEMORY_JOBS:
            return
        terminal = sorted(
            (j for j in self._jobs.values()
             if j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)),
            key=lambda j: j.completed_at or j.created_at,
        )
        to_evict = len(self._jobs) - self.MAX_IN_MEMORY_JOBS
        for job in terminal[:to_evict]:
            self._jobs.pop(job.id, None)
        logger.info(f"Evicted {min(to_evict, len(terminal))} completed jobs from memory")

    def get_stats(self) -> Dict[str, Any]:
        jobs = list(self._jobs.values())
        return {
            "total": len(jobs),
            "pending": sum(1 for j in jobs if j.status == JobStatus.PENDING),
            "queued": sum(1 for j in jobs if j.status == JobStatus.QUEUED),
            "running": sum(1 for j in jobs if j.status == JobStatus.RUNNING),
            "waiting_approval": sum(1 for j in jobs if j.status == JobStatus.WAITING_APPROVAL),
            "completed": sum(1 for j in jobs if j.status == JobStatus.COMPLETED),
            "failed": sum(1 for j in jobs if j.status == JobStatus.FAILED),
            "cancelled": sum(1 for j in jobs if j.status == JobStatus.CANCELLED),
            "queue_size": self._queue.qsize(),
        }
