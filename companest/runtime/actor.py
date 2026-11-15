"""Single-job workload hosted by Substrate, with no local worker queue.

The image/configuration is company-scoped by the operator. Only the trusted
Companest control plane may reach this listener through the Substrate router.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..config import CompanestConfig
from .harness import ActorOrchestrator
from dataclasses import asdict
from ..utils import run_sync
from .storage import FileLock, check_identity
from .services import ActorServices


class TaskRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1)
    company_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    storage_id: str = Field(min_length=1)
    workspace_storage_id: str | None = None
    capability: str = Field(min_length=32, repr=False)

    model_config = {"extra": "forbid"}


class ActorWorkload:
    def __init__(self, config: CompanestConfig, company_id: str | None, journal: Path, storage_id: str):
        self.config = config
        self.company_id = company_id
        self.journal = journal
        self.storage_id = storage_id
        self.record: dict | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def start(self):
        if not self.config.storage_lock_dir:
            raise ValueError("Actor requires storage_lock_dir on a persistent shared mount")
        await run_sync(check_identity, Path(self.config.storage_lock_dir), self.storage_id)
        if not self.config.control_plane_url:
            raise ValueError("Actor requires control_plane_url for budget and schedule services")
        companies = Path(self.config.data_dir) / "companies"
        if companies.exists() and any(p.is_dir() and p.name != self.company_id for p in companies.iterdir()):
            raise ValueError("Actor filesystem exposes another company's directory")
        if self.journal.exists():
            self.record = json.loads(await run_sync(self.journal.read_text))
            if self.record.get("company_id") != self.company_id:
                raise ValueError("Task journal belongs to a different company scope")
            if self.record.get("status") not in ("running", "completed", "failed"):
                raise ValueError("Task journal has an invalid status")
            if self.record["status"] == "running":
                # A cold process restart cannot recover an in-flight coroutine.
                # A memory snapshot resumes the existing process instead.
                self.record.update(
                    status="failed", error="Actor cold-started during execution; outcome may be unknown",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                await self.persist()

    async def persist(self):
        data = json.dumps(self.record, ensure_ascii=False)

        def write():
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.journal.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.journal)
            directory = os.open(self.journal.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        await run_sync(write)

    async def submit(self, request: TaskRequest):
        if request.storage_id != self.storage_id:
            raise HTTPException(status_code=409, detail="Actor persistent storage identity mismatch")
        if request.company_id != self.company_id or request.context.get("company_id", self.company_id) != self.company_id:
            raise HTTPException(status_code=403, detail="Task company does not match this actor's scope")
        digest = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True).encode()).hexdigest()
        async with self.lock:
            if self.record:
                if self.record["job_id"] != request.job_id or self.record["request_hash"] != digest:
                    raise HTTPException(status_code=409, detail="Actor already belongs to a different task")
                return self.public_record()
            self.record = {
                "job_id": request.job_id, "request_hash": digest,
                "company_id": request.company_id, "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None, "result": None, "error": None,
            }
            try:
                await self.persist()
            except Exception:
                self.record = None
                raise
            self.task = asyncio.create_task(self.execute(request))
            return self.public_record()

    def public_record(self):
        return {key: value for key, value in self.record.items() if key != "request_hash"}

    async def execute(self, request: TaskRequest):
        try:
            async with asyncio.timeout(self.config.global_timeout):
                orchestrator = ActorOrchestrator(self.config)
                await orchestrator.init_teams_async(self.config.data_dir)
                self.validate_scope(orchestrator)
                services = ActorServices(self.config.control_plane_url, request.job_id, request.capability)
                orchestrator.cost_gate = services
                orchestrator.user_scheduler = services
                orchestrator.events.on_any(services.emit)
                if self.company_id:
                    orchestrator.dreamer.log_path = (
                        Path(self.config.data_dir) / "companies" / self.company_id / "dream_log.json"
                    )
                context = dict(request.context)
                context["company_id"] = self.company_id
                team_id = context.get("team_id")
                with ExitStack() as locks:
                    workspace_id = context.get("workspace_id")
                    if workspace_id:
                        ws = orchestrator.workspace_registry.get(workspace_id)
                        if not ws or ws.storage_id != request.workspace_storage_id:
                            raise ValueError("Actor workspace is missing or has a different storage identity")
                        await run_sync(ws.validate_storage, self.company_id)
                        try:
                            key = hashlib.sha256(ws.storage_id.encode()).hexdigest()
                            locks.enter_context(FileLock(
                                Path(self.config.storage_lock_dir) / f"workspace-{key}.lock", blocking=False,
                            ))
                        except BlockingIOError as exc:
                            raise ValueError("Workspace is busy with another task") from exc
                    if context.get("maintenance"):
                        result = await self.maintain_memory(orchestrator, services, context["maintenance"])
                    elif team_id:
                        result = await orchestrator.run_team(request.task, team_id, mode=context.get("mode"), user_context=context)
                    else:
                        result, routing = await orchestrator.run_auto(request.task, mode=context.get("mode"), user_context=context)
                        self.record["routing"] = asdict(routing)
            self.record.update(status="completed", result=result)
        except asyncio.CancelledError:
            self.record.update(status="failed", error="Actor process stopped during execution; outcome may be unknown")
            raise
        except Exception as exc:
            self.record.update(status="failed", error=str(exc))
        finally:
            self.record["completed_at"] = datetime.now(timezone.utc).isoformat()
            await self.persist()

    async def maintain_memory(self, orchestrator, services, operation):
        teams = [tid for tid in orchestrator.team_registry.list_teams()
                 if orchestrator.can_access_team(self.company_id, tid)]
        if not teams:
            raise ValueError("No authorized team for memory maintenance")
        dreamer = orchestrator.dreamer
        call_llm = dreamer._call_llm

        async def authorized_call(prompt):
            decision = await services.evaluate(prompt, teams[0], dreamer.model, priority="low")
            if decision.action == "rejected":
                raise ValueError("CostGate rejected memory maintenance: " + decision.reason)
            return await call_llm(prompt)

        dreamer._call_llm = authorized_call
        if operation == "dream":
            result = await dreamer.run_all_dreams()
        elif operation == "consolidate":
            result = await dreamer.run_deep_consolidation()
        else:
            raise ValueError("Unknown memory maintenance operation")
        return json.dumps(result, ensure_ascii=False)

    def validate_scope(self, orchestrator):
        """Fail closed on an accidentally over-broad operator mount."""
        companies = orchestrator.company_registry.list_companies()
        if any(cid != self.company_id for cid in companies):
            raise ValueError("Actor filesystem exposes another company's configuration")
        if self.company_id:
            company = orchestrator.company_registry.get(self.company_id)
            if not company or not company.enabled:
                raise ValueError("Actor company is missing or disabled")
        for tid in orchestrator.team_registry.list_teams():
            if tid == f"company-{self.company_id}":
                continue
            if not orchestrator.can_access_team(self.company_id, tid):
                raise ValueError("Actor filesystem exposes an unauthorized team")
        for wid in orchestrator.workspace_registry.list_workspaces():
            if orchestrator.workspace_registry.get(wid).company_id != self.company_id:
                raise ValueError("Actor filesystem exposes another company's workspace")


def create_app(workload: ActorWorkload) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await workload.start()
        yield
        if workload.task and not workload.task.done():
            workload.task.cancel()
            await asyncio.gather(workload.task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/readyz")
    async def ready():
        return {"ready": True}

    @app.post("/task", status_code=202)
    async def submit(request: TaskRequest):
        return await workload.submit(request)

    @app.get("/task")
    async def status():
        if workload.record is None:
            raise HTTPException(status_code=404, detail="No task has been submitted")
        return workload.public_record()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--company", required=True, help="Company ID, or @shared for the shared scope")
    parser.add_argument("--journal", type=Path, default=Path("/data/task.json"))
    parser.add_argument("--storage-id", required=True, help="Identity of the mounted persistent storage")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    config = (CompanestConfig.from_markdown(args.config) if args.config.suffix == ".md"
              else CompanestConfig.from_json_file(args.config))
    company_id = None if args.company == "@shared" else args.company
    import uvicorn
    uvicorn.run(create_app(ActorWorkload(config, company_id, args.journal, args.storage_id)), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
