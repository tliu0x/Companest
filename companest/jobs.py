"""
Companest Job Manager

Manages the lifecycle of jobs submitted to the Companest control panel.
Jobs flow through the pipeline: PENDING  QUEUED  DISPATCHED  RUNNING  COMPLETED/FAILED/CANCELLED

Persistence via aiosqlite to .companest/jobs.db.

Usage:
    manager = JobManager(orchestrator)
    await manager.start()

    job_id = await manager.submit("Analyze this codebase")
    job = await manager.get_job(job_id)

    await manager.stop()
"""

import uuid
import asyncio
import logging
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .exceptions import JobError

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job lifecycle status"""
    PENDING = "pending"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Valid state transitions  any transition not listed here is a bug
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
    """
    Represents a job submitted to the Companest control panel.

    Attributes:
        id: Unique job identifier
        task: The task description/prompt
        status: Current job status
        context: Additional context for the task
        subtasks: List of subtask dicts (from orchestrator)
        result: Final result text
        error: Error message if failed
        created_at: When the job was submitted
        started_at: When execution began
        completed_at: When execution finished
        submitted_by: Who submitted the job (e.g., "n8n", "cli", "api")
    """
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
        """Get job duration in milliseconds"""
        if self.started_at and self.completed_at:
            delta = self.completed_at - self.started_at
            return int(delta.total_seconds() * 1000)
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
    """
    Manages the lifecycle of jobs in the Companest control panel.

    Features:
    - Async job queue with configurable worker count
    - Job persistence to SQLite via aiosqlite
    - Job cancellation support
    - Status tracking and querying

    Example:
        manager = JobManager(orchestrator, data_dir=Path(".companest"))
        await manager.start(num_workers=3)

        job_id = await manager.submit("Generate unit tests for auth module")
        job = await manager.get_job(job_id)

        jobs = await manager.list_jobs(status=JobStatus.COMPLETED)
        await manager.stop()
    """

    MAX_IN_MEMORY_JOBS = 500

    def __init__(self, orchestrator=None, data_dir: Optional[Path] = None):
        """
        Initialize the JobManager.

        Args:
            orchestrator: CompanestOrchestrator instance for executing jobs
            data_dir: Directory for job database
        """
        self.orchestrator = orchestrator
        self._data_dir = data_dir or Path(".companest")
        self._jobs: Dict[str, Job] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._db = None

    def _transition(self, job: Job, new_status: JobStatus) -> None:
        """Validate and apply a job state transition. Logs every change."""
        old = job.status
        allowed = VALID_TRANSITIONS.get(old, frozenset())
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
        """
        Start the job manager with worker tasks.

        Args:
            num_workers: Number of concurrent worker tasks
        """
        if self._running:
            return

        self._running = True

        # Initialize database
        await self._init_db()
        await self._load_jobs()

        # Start workers
        for i in range(num_workers):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self._workers.append(task)

        # Recover interrupted jobs: RUNNING/DISPATCHED -> QUEUED
        for job in self._jobs.values():
            if job.status in (JobStatus.RUNNING, JobStatus.DISPATCHED):
                logger.warning(
                    f"Recovering interrupted job {job.id} "
                    f"({job.status.value} -> queued)"
                )
                job.status = JobStatus.QUEUED  # bypass _transition (not a normal flow)
                job.started_at = None
                await self._persist_job(job)

        # Re-queue any pending/queued jobs from persistence
        for job in self._jobs.values():
            if job.status == JobStatus.PENDING:
                self._transition(job, JobStatus.QUEUED)
                await self._queue.put(job.id)
            elif job.status == JobStatus.QUEUED:
                await self._queue.put(job.id)

        logger.info(f"JobManager started with {num_workers} workers")

    async def stop(self) -> None:
        """Stop the job manager and cancel all workers."""
        self._running = False

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass

        self._workers.clear()

        # Close database
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
        """
        Submit a new job.

        Args:
            task: The task description/prompt
            context: Optional context dict
            submitted_by: Source identifier

        Returns:
            Job ID
        """
        ctx = dict(context or {})
        # Resolve company_id: explicit param takes priority, then context
        effective_company_id = company_id or ctx.get("company_id")
        if company_id and ctx.get("company_id") and company_id != ctx.get("company_id"):
            raise JobError(
                f"company_id mismatch: param={company_id}, context={ctx.get('company_id')}"
            )
        if effective_company_id and "company_id" not in ctx:
            # Keep the runtime execution context aligned with the indexed company_id field.
            ctx["company_id"] = effective_company_id
        job = Job(
            id=str(uuid.uuid4()),
            task=task,
            status=JobStatus.PENDING,
            context=ctx,
            submitted_by=submitted_by,
            company_id=effective_company_id,
        )

        self._jobs[job.id] = job
        await self._persist_job(job)

        # Queue for execution
        self._transition(job, JobStatus.QUEUED)
        await self._queue.put(job.id)

        logger.info(f"Job {job.id} submitted by {submitted_by}: {task[:80]}...")
        return job.id

    async def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    async def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        company_id: Optional[str] = None,
    ) -> List[Job]:
        """
        List jobs, optionally filtered by status.

        Args:
            status: Filter by job status
            limit: Max results to return
            offset: Pagination offset
        """
        jobs = list(self._jobs.values())

        if status:
            jobs = [j for j in jobs if j.status == status]
        if company_id:
            jobs = [j for j in jobs if j.company_id == company_id]

        # Sort by creation time, newest first
        jobs.sort(key=lambda j: j.created_at, reverse=True)

        return jobs[offset:offset + limit]

    async def count_jobs(
        self,
        status: Optional[JobStatus] = None,
        company_id: Optional[str] = None,
    ) -> int:
        """Return the total count of jobs matching the given filters."""
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if company_id:
            jobs = [j for j in jobs if j.company_id == company_id]
        return len(jobs)

    async def pause_for_approval(self, job_id: str, reason: str) -> bool:
        """
        Pause a running job to wait for human approval.

        Args:
            job_id: Job ID to pause
            reason: Why approval is needed

        Returns:
            True if paused successfully
        """
        job = self._jobs.get(job_id)
        if not job:
            raise JobError(f"Job not found: {job_id}")

        self._transition(job, JobStatus.WAITING_APPROVAL)
        job.approval_reason = reason
        await self._persist_job(job)

        logger.info(f"Job {job_id} paused for approval: {reason}")
        return True

    async def resume_job(self, job_id: str, approved: bool, feedback: Optional[str] = None) -> bool:
        """
        Resume a job that is waiting for approval.

        Args:
            job_id: Job ID to resume
            approved: Whether the job is approved to continue
            feedback: Optional feedback to inject into job context

        Returns:
            True if resumed/cancelled successfully
        """
        job = self._jobs.get(job_id)
        if not job:
            raise JobError(f"Job not found: {job_id}")
        if job.status != JobStatus.WAITING_APPROVAL:
            raise JobError(
                f"Job {job_id} is not waiting for approval (status: {job.status.value})"
            )

        if not approved:
            self._transition(job, JobStatus.CANCELLED)
            job.completed_at = datetime.now(timezone.utc)
            job.error = feedback or "Approval denied"
            await self._persist_job(job)
            logger.info(f"Job {job_id} denied and cancelled")
            return True

        # Approved  transition back to RUNNING and re-queue
        if feedback:
            job.context["approval_feedback"] = feedback
        job.approval_reason = None
        self._transition(job, JobStatus.RUNNING)
        await self._persist_job(job)

        # Re-queue for worker to pick up
        await self._queue.put(job.id)
        logger.info(f"Job {job_id} approved and resumed")
        return True

    async def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a pending or running job.

        Args:
            job_id: Job ID to cancel

        Returns:
            True if cancelled, False if not cancellable
        """
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
        """Worker task that processes jobs from the queue."""
        try:
            while self._running:
                try:
                    job_id = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                job = self._jobs.get(job_id)
                if not job or job.status == JobStatus.CANCELLED:
                    continue

                # Resumed jobs are already RUNNING  skip dispatch transitions
                if job.status == JobStatus.RUNNING:
                    logger.info(f"[{name}] Resuming job {job_id}")
                else:
                    logger.info(f"[{name}] Processing job {job_id}")
                try:
                    await self._execute_job(job, resumed=(job.status == JobStatus.RUNNING))
                except asyncio.CancelledError:
                    # Shutdown while job is running — mark as FAILED
                    if job.status == JobStatus.RUNNING:
                        job.status = JobStatus.FAILED
                        job.error = "Interrupted by shutdown"
                        job.completed_at = datetime.now(timezone.utc)
                        await self._persist_job(job)
                        logger.warning(f"[{name}] Job {job_id} interrupted by shutdown")
                    raise

        except asyncio.CancelledError:
            return

    async def _execute_job(self, job: Job, resumed: bool = False) -> None:
        """Execute a single job through the orchestrator."""
        if not resumed:
            self._transition(job, JobStatus.DISPATCHED)
            job.started_at = datetime.now(timezone.utc)
            await self._persist_job(job)

        if not self.orchestrator:
            self._transition(job, JobStatus.FAILED)
            job.error = "No orchestrator configured"
            job.completed_at = datetime.now(timezone.utc)
            await self._persist_job(job)
            return

        try:
            if not resumed:
                self._transition(job, JobStatus.RUNNING)
                await self._persist_job(job)

            # Ensure company_id from the job record is in the execution context
            exec_context = dict(job.context) if job.context else {}
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

            self._transition(job, JobStatus.COMPLETED)
            job.result = result_text
            job.completed_at = datetime.now(timezone.utc)

        except Exception as e:
            self._transition(job, JobStatus.FAILED)
            job.error = str(e)
            job.completed_at = datetime.now(timezone.utc)
            logger.error(f"Job {job.id} failed: {e}")

        await self._persist_job(job)
        self._evict_completed_jobs()

    # -------------------------------------------------------------------------
    # Persistence (aiosqlite)
    # -------------------------------------------------------------------------

    async def _init_db(self) -> None:
        """Initialize the SQLite database."""
        try:
            import aiosqlite
        except ImportError:
            logger.warning(
                "aiosqlite not installed, job persistence disabled. "
                "Install with: pip install aiosqlite"
            )
            return

        self._data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._data_dir / "jobs.db"

        self._db = await aiosqlite.connect(str(db_path))
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

        # Migration: add approval_reason column if missing (existing DBs)
        try:
            await self._db.execute("SELECT approval_reason FROM jobs LIMIT 1")
        except Exception:
            await self._db.execute("ALTER TABLE jobs ADD COLUMN approval_reason TEXT")
            await self._db.commit()

        # Migration: add company_id column if missing (existing DBs)
        try:
            await self._db.execute("SELECT company_id FROM jobs LIMIT 1")
        except Exception:
            await self._db.execute("ALTER TABLE jobs ADD COLUMN company_id TEXT")
            await self._db.commit()

    async def _persist_job(self, job: Job) -> None:
        """Save/update a job in the database."""
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
        """Load jobs from database."""
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
                    company_id=row[12] if len(row) > 12 else None,
                )
                self._jobs[job.id] = job

            logger.info(f"Loaded {len(rows)} jobs from database")
        except Exception as e:
            logger.error(f"Failed to load jobs: {e}")

    def _evict_completed_jobs(self) -> None:
        """Evict oldest completed/failed/cancelled jobs when over the cap."""
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
        if to_evict > 0:
            logger.info(f"Evicted {min(to_evict, len(terminal))} completed jobs from memory")

    def get_stats(self) -> Dict[str, Any]:
        """Get job statistics."""
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
