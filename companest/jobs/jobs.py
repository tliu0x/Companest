import uuid
import secrets
import hmac
import os
from dataclasses import asdict
import asyncio
import logging
import json
from typing import Dict, List, Optional, Any, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from ..exceptions import JobError
from ..runtime.substrate import ActorRef, SubstrateClient, SubstrateError, ActorNotFound
from ..runtime.storage import FileLock, check_identity
from ..utils import run_sync

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

    execution: Dict[str, Any] = field(default_factory=dict)
    capability: str = field(default="", repr=False)

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
            "execution": dict(self.execution),
        }


class JobManager:
    """Durable business records referencing Substrate actors; no local workers.

    One manager owns a database. Restart reconnects to saved references and never
    replays legacy jobs or creates replacements for missing execution instances.
    """

    TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    def __init__(self, orchestrator=None, data_dir: Optional[Path] = None, *, client=None):
        self.orchestrator = orchestrator
        config = getattr(orchestrator, "config", None)
        default_dir = (config.control_state_dir or config.data_dir) if config else ".companest"
        self._data_dir = Path(data_dir or default_dir)
        self._jobs: Dict[str, Job] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._db = None
        self._authorization_lock = asyncio.Lock()
        self._submission_lock = asyncio.Lock()
        self._owner = None
        self.settings = getattr(getattr(orchestrator, "config", None), "substrate", None)
        if client is None and self.settings:
            client = SubstrateClient(
                executable=self.settings.executable, kubeconfig=self.settings.kubeconfig,
                context=self.settings.context, token_file=self.settings.token_file,
                timeout=self.settings.timeout,
            )
        self.client = client
        if orchestrator is not None:
            orchestrator.job_manager = self

    async def start(self) -> None:
        if self._db:
            return
        if not self.settings or not self.client:
            raise JobError("Substrate is required: configure substrate.scopes and persistent storage")
        lock_dir = self.orchestrator.config.storage_lock_dir
        if not lock_dir:
            raise JobError("storage_lock_dir must identify the shared persistent lock mount")
        await run_sync(check_identity, Path(lock_dir), self.settings.storage_id)
        self._owner = FileLock(self._data_dir / ".jobs-owner.lock", blocking=False)
        try:
            self._owner.__enter__()
        except BlockingIOError as exc:
            self._owner = None
            raise JobError("Another control plane owns this job database") from exc
        try:
            await self._init_db()
            await self._load_jobs()
            for job in self._jobs.values():
                if job.status not in self.TERMINAL and not job.execution:
                    job.status = JobStatus.FAILED
                    job.error = "Legacy execution has no actor reference; outcome unknown, not replayed"
                    job.completed_at = datetime.now(timezone.utc)
                    await self._persist_job(job)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        # Closing the API never terminates work in Substrate.
        if self._db:
            await self._db.close()
            self._db = None
        if self._owner:
            self._owner.__exit__(None, None, None)
            self._owner = None

    async def submit(self, task: str, context=None, submitted_by="api", company_id=None, idempotency_key=None) -> str:
        if not self._db:
            raise JobError("JobManager has not started")
        ctx = dict(context or {})
        if company_id and ctx.get("company_id") and company_id != ctx["company_id"]:
            raise JobError("company_id mismatch between request and context")
        if company_id:
            ctx["company_id"] = company_id
        ctx = await self.orchestrator.prepare_submission(ctx)
        company_id = ctx.get("company_id")
        binding = self.settings.scopes.get(company_id or "@shared")
        if binding is None:
            raise JobError("No Substrate actor template configured for this company scope")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
            raise JobError("idempotency_key must contain 1 to 128 characters")
        job_id = (str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([company_id, submitted_by, idempotency_key])))
                  if idempotency_key else str(uuid.uuid4()))
        async with self._submission_lock:
            existing = self._jobs.get(job_id)
            if existing:
                if existing.task != task or existing.context != ctx:
                    raise JobError("Idempotency key already used with a different task/context")
                return job_id
            job = Job(id=job_id, task=task, context=ctx,
                      company_id=company_id, submitted_by=submitted_by, status=JobStatus.DISPATCHED,
                      capability=secrets.token_urlsafe(32))
            workspace = self.orchestrator.workspace_registry.get(ctx["workspace_id"]) if ctx.get("workspace_id") else None
            job.execution = {
                "atespace": binding.atespace, "name": "job-" + job.id,
                "template": binding.template, "phase": "creating",
                "storage_id": self.settings.storage_id,
                "workspace_storage_id": workspace.storage_id if workspace else None,
            }
            # Record intent before creating an actor or issuing any task request.
            await self._persist_job(job)
            self._jobs[job.id] = job
        async with self._locks.setdefault(job.id, asyncio.Lock()):
            try:
                resource = await self.client.create_actor(self._ref(job), binding.template)
                self._bind(job, resource)
                await self._persist_job(job)
                await self._send(job)
            except (SubstrateError, ValueError) as exc:
                job.execution["error"] = str(exc)
                await self._persist_job(job)
        return job.id

    @staticmethod
    def _ref(job):
        return ActorRef(job.execution["atespace"], job.execution["name"])

    @staticmethod
    def _bind(job, resource):
        meta = resource.get("metadata", {})
        template = resource.get("actorTemplate", {})
        if (meta.get("name") != job.execution["name"] or
                meta.get("atespace") != job.execution["atespace"] or
                template.get("name") != job.execution["template"] or not meta.get("uid")):
            raise SubstrateError("Actor identity/template mismatch")
        if job.execution.get("uid") and job.execution["uid"] != meta["uid"]:
            raise SubstrateError("Actor was replaced; refusing to attach or replay")
        job.execution["uid"] = meta["uid"]
        if job.execution["phase"] == "creating":
            job.execution["phase"] = "created"

    async def _send(self, job):
        job.execution["phase"] = "submitting"
        await self._persist_job(job)
        response = await self.client.request(self._ref(job), "POST", "/task", port=8080, payload={
            "job_id": job.id, "task": job.task, "company_id": job.company_id,
            "context": job.context, "storage_id": job.execution["storage_id"],
            "workspace_storage_id": job.execution.get("workspace_storage_id"),
            "capability": job.capability,
        })
        await self._accept(job, response)

    async def _accept(self, job, response):
        if response.status_code != 200 and response.status_code != 202:
            raise SubstrateError(f"Actor task request returned HTTP {response.status_code}; execution not retried")
        record = response.json()
        if not isinstance(record, dict):
            raise SubstrateError("Actor returned an invalid job record")
        if record.get("job_id") != job.id or record.get("company_id") != job.company_id:
            raise SubstrateError("Actor returned a different job/company")
        if record.get("status") not in ("running", "completed", "failed"):
            raise SubstrateError("Actor returned an invalid job status")
        if record["status"] != "running" and not record.get("completed_at"):
            raise SubstrateError("Actor terminal record is missing its completion time")
        if record["status"] == "completed" and not isinstance(record.get("result"), str):
            raise SubstrateError("Actor completed without a full result")
        started_at = datetime.fromisoformat(record["started_at"])
        completed_at = datetime.fromisoformat(record["completed_at"]) if record.get("completed_at") else None
        job.status = JobStatus(record["status"])
        job.started_at, job.completed_at = started_at, completed_at
        job.result, job.error = record.get("result"), record.get("error")
        job.execution["phase"] = "accepted"
        job.execution.pop("error", None)
        if "routing" in record:
            job.execution["routing"] = record["routing"]
        await self._persist_job(job)

    async def get_job(self, job_id: str) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        async with self._locks.setdefault(job.id, asyncio.Lock()):
            if not job.execution or job.execution.get("deleted"):
                return job
            try:
                # A live reference must still identify the same instance.
                resource = await self.client.get_actor(self._ref(job))
                self._bind(job, resource)
                if job.execution.get("cancel_requested"):
                    await self._cancel(job)
                elif job.status not in self.TERMINAL:
                    if job.execution["phase"] == "created":
                        await self._send(job)
                    else:
                        response = await self.client.request(self._ref(job), "GET", "/task", port=8080)
                        await self._accept(job, response)
                if job.status in self.TERMINAL and not job.execution.get("deleted"):
                    # Also covers a previous failed persistence attempt: never
                    # trust only the in-memory terminal projection for cleanup.
                    await self._persist_job(job)
                    await self.client.delete_actor(self._ref(job))
                    job.execution["deleted"] = True
                    job.execution.pop("error", None)
            except ActorNotFound:
                if job.execution.get("phase") == "creating" and not job.execution.get("cancel_requested"):
                    job.execution["error"] = "Actor creation is unconfirmed; query again, do not recreate"
                    await self._persist_job(job)
                    return job
                elif job.execution.get("cancel_requested"):
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now(timezone.utc)
                elif job.status not in self.TERMINAL:
                    job.status = JobStatus.FAILED
                    job.error = "Actor disappeared; outcome unknown, task not replayed"
                    job.completed_at = datetime.now(timezone.utc)
                job.execution["deleted"] = True
                job.execution.pop("error", None)
            except (SubstrateError, ValueError, KeyError, TypeError) as exc:
                # Transport failure is not evidence that a running task failed.
                job.execution["error"] = str(exc)
            await self._persist_job(job)
        return job

    def authenticate_actor(self, job_id, token):
        job = self._jobs.get(job_id)
        if (not job or not job.capability or not isinstance(token, str)
                or not hmac.compare_digest(job.capability.encode(), token.encode())):
            raise JobError("Invalid or inactive actor capability")
        self.check_actor_scope(job)
        return job

    def check_actor_scope(self, job, team_id=None):
        if job.status in self.TERMINAL or job.execution.get("cancel_requested"):
            raise JobError("Invalid or inactive actor capability")
        company = self.orchestrator.company_registry.get(job.company_id) if job.company_id else None
        if job.company_id and (company is None or not company.enabled):
            raise JobError("Company is no longer enabled")
        if team_id and (not self.orchestrator.can_access_team(job.company_id, team_id)
                        or not self.orchestrator.team_registry.get_config(team_id)):
            raise JobError("Team is outside this job's current company scope")
        return company

    async def authorize(self, job, payload):
        """Authorize and charge an estimate before execution, including failures.

        Serializing the check and reservation prevents two actors from spending
        the same remaining budget. The durable spend log is authoritative.
        """
        team_id = payload["team_id"]
        async with self._authorization_lock:
            company = self.check_actor_scope(job, team_id)
            config = self.orchestrator.team_registry.get_config(team_id)
            lead = next((p for p in config.pis if p.id == config.lead_pi), next(iter(config.pis), None))
            model = lead.model if lead else "deepseek-chat"
            # A task capability cannot grant itself the critical budget bypass.
            priorities = ("low", "normal", "high", "critical")
            requested = payload.get("priority", "normal")
            ceiling = job.context.get("priority", "normal")
            if requested not in priorities or ceiling not in priorities:
                raise JobError("Invalid task priority")
            priority = priorities[min(priorities.index(requested), priorities.index(ceiling))]
            gate = self.orchestrator.cost_gate
            decision = await gate.evaluate(
                payload["task"], team_id, model, priority=priority,
                company_id=job.company_id,
                company_budget_hourly=company.preferences.budget_hourly_usd if company else None,
            )
            # Approval can wait for a human. Recheck revocation after that wait.
            self.check_actor_scope(job, team_id)
            if decision.action != "rejected":
                estimate = decision.estimate
                await run_sync(gate.record_spending, team_id, payload["task"],
                    {"input": estimate.estimated_input_tokens, "output": estimate.estimated_output_tokens},
                    estimate.estimated_cost_usd, company_id=job.company_id)
            return asdict(decision)

    async def _cancel(self, job):
        try:
            await self.client.delete_actor(self._ref(job))
        except ActorNotFound:
            pass
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc)
        job.execution["deleted"] = True
        job.execution.pop("error", None)
        await self._persist_job(job)

    async def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobError(f"Job not found: {job_id}")
        async with self._locks.setdefault(job.id, asyncio.Lock()):
            if job.status in self.TERMINAL:
                return False
            job.execution["cancel_requested"] = True
            await self._persist_job(job)
            # Never report cancellation until Substrate confirms deletion.
            try:
                resource = await self.client.get_actor(self._ref(job))
                self._bind(job, resource)
            except ActorNotFound:
                pass
            await self._cancel(job)
        return True

    async def wait(self, job_id):
        while True:
            job = await self.get_job(job_id)
            if job.status == JobStatus.COMPLETED:
                return job
            if job.status in self.TERMINAL:
                raise JobError(job.error or f"Job {job_id} {job.status.value}")
            if job.execution.get("error"):
                raise JobError(f"Job {job_id} is reconnectable: {job.execution['error']}")
            await asyncio.sleep(self.settings.poll_interval)

    def _matching(self, status, company_id):
        return [j for j in self._jobs.values()
                if (not status or j.status == status) and (not company_id or j.company_id == company_id)]

    async def list_jobs(self, status=None, limit=50, offset=0, company_id=None):
        # Lists are the durable last-observed projection. Never fan out dozens
        # of network requests while an operator is diagnosing an outage.
        jobs = sorted(self._matching(status, company_id), key=lambda j: j.created_at, reverse=True)
        return jobs[offset:offset + limit]

    async def count_jobs(self, status=None, company_id=None):
        return len(self._matching(status, company_id))

    def get_stats(self):
        counts = {status.value: sum(j.status == status for j in self._jobs.values()) for status in JobStatus}
        return {"total": len(self._jobs), **counts, "queue_size": 0, "execution_backend": "substrate"}

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

    async def _init_db(self) -> None:
        try:
            import aiosqlite
        except ImportError:
            raise JobError("Install Companest with the server extra; job persistence is required")

        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = await aiosqlite.connect(str(self._data_dir / "jobs.db"))
        os.chmod(self._data_dir / "jobs.db", 0o600)
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
        for column in ("approval_reason", "company_id", "execution", "capability"):
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
                 company_id, execution, capability)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(job.execution),
                    job.capability,
                ),
            )
            await self._db.commit()
        except Exception as e:
            raise JobError(f"Failed to persist job {job.id}") from e

    async def _load_jobs(self) -> None:
        if not self._db:
            return

        try:
            async with self._db.execute(
                "SELECT id, task, status, context, subtasks, result, error, "
                "approval_reason, created_at, started_at, completed_at, submitted_by, "
                "company_id, execution, capability "
                "FROM jobs ORDER BY created_at DESC"
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
                    execution=json.loads(row[13]) if row[13] else {},
                    capability=row[14] or "",
                )
                self._jobs[job.id] = job

            logger.info(f"Loaded {len(rows)} jobs from database")
        except Exception as e:
            raise JobError("Failed to load jobs") from e
