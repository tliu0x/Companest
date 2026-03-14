"""
Companest FastAPI Control Panel Server

Provides HTTP API endpoints for managing Pi Agent Teams,
submitting jobs, and monitoring system health.

Designed to be consumed by n8n via webhooks and HTTP requests.

Usage:
    server = CompanestAPIServer(config, job_manager, orchestrator)
    app = server.create_app()
"""

import logging
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from .config import CompanestConfig
from .jobs import JobManager, JobStatus
from .orchestrator import CompanestOrchestrator
from .company import _SAFE_ID_RE
from .exceptions import CompanestError, JobError, OrchestratorError

logger = logging.getLogger(__name__)


class CompanestAPIServer:
    """
    FastAPI-based control panel server for Companest.

    Endpoints:
    - POST    /api/jobs              - Submit job
    - GET     /api/jobs/{id}         - Job status
    - GET     /api/jobs              - List jobs
    - POST    /api/jobs/{id}/cancel  - Cancel job
    - GET     /api/fleet/status      - Fleet overview
    - WS      /ws/events             - Real-time event stream
    - POST    /api/webhooks/n8n      - n8n webhook trigger
    - GET     /health                - API health check
    """

    def __init__(
        self,
        config: CompanestConfig,
        job_manager: JobManager,
        orchestrator: Optional[CompanestOrchestrator] = None,
    ):
        self.config = config
        self.job_manager = job_manager
        self.orchestrator = orchestrator
        self._app = None
        self._event_subscribers: List[asyncio.Queue] = []
        self.MAX_WS_SUBSCRIBERS = 50

    def create_app(self):
        """Create and configure the FastAPI application."""
        try:
            from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
            from fastapi.middleware.cors import CORSMiddleware
            from pydantic import BaseModel, Field
        except ImportError:
            raise ImportError(
                "FastAPI required. Install with: pip install fastapi uvicorn"
            )

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(application):
            # Startup: start scheduler if available
            if self.orchestrator and hasattr(self.orchestrator, "scheduler"):
                await self.orchestrator.scheduler.start()
                logger.info("Scheduler started via lifespan")
            # Subscribe to EventBus  forward to WebSocket subscribers
            if self.orchestrator and hasattr(self.orchestrator, "events"):
                self.orchestrator.events.on_any(self._on_event_bus)
                logger.info("Server subscribed to EventBus")
            yield
            # Shutdown: stop scheduler and close feed client
            if self.orchestrator and hasattr(self.orchestrator, "scheduler"):
                await self.orchestrator.scheduler.stop()
                logger.info("Scheduler stopped via lifespan")
            from .feeds import close_client as close_feed_client
            await close_feed_client()

        app = FastAPI(
            title="Companest Control Panel",
            description="Companest Fleet Management API",
            version="1.0.0",
            lifespan=lifespan,
        )

        # CORS: use configured origins, or no CORS if empty
        allowed_origins = self.config.api.allowed_origins or []
        if allowed_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=allowed_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        # Authentication middleware
        api_token = self.config.api.auth_token
        if api_token:
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            class AuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    # Skip auth for health check and admin UI
                    if request.url.path == "/health" or request.url.path.startswith(("/admin", "/_nicegui")):
                        return await call_next(request)
                    # Check bearer token
                    auth_header = request.headers.get("Authorization", "")
                    token = auth_header.removeprefix("Bearer ").strip()
                    if token != api_token:
                        return JSONResponse(
                            {"error": "Unauthorized"},
                            status_code=401,
                        )
                    return await call_next(request)

            app.add_middleware(AuthMiddleware)
        else:
            import os
            # In production (non-debug), refuse to start without auth token
            is_debug = getattr(self.config, "debug", False) or os.environ.get("COMPANEST_DEBUG", "").lower() in ("1", "true")
            if not is_debug:
                raise RuntimeError(
                    "COMPANEST_API_TOKEN is required for production. "
                    "Set the COMPANEST_API_TOKEN environment variable or enable debug mode. "
                    "To run without auth (development only): set debug=true in config or COMPANEST_DEBUG=1."
                )
            logger.warning(
                "No API auth token configured (COMPANEST_API_TOKEN). "
                "All endpoints are publicly accessible. "
                "This is allowed only because debug mode is enabled."
            )

        # --- Request/Response Models ---

        class SubmitJobRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            context: Optional[Dict[str, Any]] = None
            submitted_by: str = Field(default="api", max_length=100)
            company_id: Optional[str] = Field(default=None, max_length=100)

        class WebhookRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            context: Optional[Dict[str, Any]] = None
            callback_url: Optional[str] = Field(default=None, max_length=2000)

        # --- Health ---

        @app.get("/health")
        async def health_check():
            return {
                "status": "ok",
                "service": "companest-control-panel",
                "version": "1.0.0",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # --- Job Management ---

        @app.post("/api/jobs")
        async def submit_job(req: SubmitJobRequest):
            try:
                job_id = await self.job_manager.submit(
                    task=req.task,
                    context=req.context,
                    submitted_by=req.submitted_by,
                    company_id=req.company_id,
                )
                return {"job_id": job_id, "status": "queued"}
            except JobError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                logger.error(f"Job submission failed: {e}")
                raise HTTPException(status_code=500, detail="Internal server error")

        @app.get("/api/jobs/{job_id}")
        async def get_job(job_id: str):
            job = await self.job_manager.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return job.to_dict()

        @app.get("/api/jobs")
        async def list_jobs(
            status: Optional[str] = None,
            limit: int = 50,
            offset: int = 0,
        ):
            filter_status = None
            if status:
                try:
                    filter_status = JobStatus(status)
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid status: {status}",
                    )

            jobs = await self.job_manager.list_jobs(
                status=filter_status, limit=limit, offset=offset
            )
            return {
                "jobs": [j.to_dict() for j in jobs],
                "total": len(jobs),
                "stats": self.job_manager.get_stats(),
            }

        @app.post("/api/jobs/{job_id}/cancel")
        async def cancel_job(job_id: str):
            try:
                cancelled = await self.job_manager.cancel_job(job_id)
                if cancelled:
                    return {"status": "cancelled", "job_id": job_id}
                return {"status": "not_cancellable", "job_id": job_id}
            except JobError as e:
                raise HTTPException(status_code=404, detail=str(e))

        # --- Fleet ---

        @app.get("/api/fleet/status")
        async def fleet_status():
            status = {
                "jobs": self.job_manager.get_stats(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self.orchestrator and hasattr(self.orchestrator, "team_registry"):
                status["teams"] = self.orchestrator.team_registry.get_fleet_status()
            # Per-company stats
            if self.orchestrator and hasattr(self.orchestrator, "company_registry"):
                companies = {}
                for cid in self.orchestrator.company_registry.list_companies():
                    cfg = self.orchestrator.company_registry.get(cid)
                    company_teams = list(
                        self.orchestrator.team_registry.get_configs_by_company(cid).keys()
                    ) if hasattr(self.orchestrator, "team_registry") else []
                    company_jobs = await self.job_manager.list_jobs(company_id=cid, limit=100)
                    companies[cid] = {
                        "name": cfg.name if cfg else cid,
                        "enabled": cfg.enabled if cfg else False,
                        "active_teams": len(company_teams),
                        "total_jobs": len(company_jobs),
                    }
                status["companies"] = companies
            return status

        # --- WebSocket Events ---

        if self.config.api.enable_websocket_events:

            @app.websocket("/ws/events")
            async def websocket_events(websocket: WebSocket):
                # Check auth token for WebSocket (middleware doesn't cover WS)
                if api_token:
                    token = websocket.query_params.get("token", "")
                    if token != api_token:
                        await websocket.close(code=4001, reason="Unauthorized")
                        return
                if len(self._event_subscribers) >= self.MAX_WS_SUBSCRIBERS:
                    await websocket.close(code=4002, reason="Too many connections")
                    return
                await websocket.accept()
                queue: asyncio.Queue = asyncio.Queue(maxsize=100)
                self._event_subscribers.append(queue)

                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                queue.get(), timeout=30.0
                            )
                            await websocket.send_json(event)
                        except asyncio.TimeoutError:
                            # Send keepalive ping
                            await websocket.send_json({"type": "ping"})
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug(f"WebSocket error: {e}")
                finally:
                    try:
                        self._event_subscribers.remove(queue)
                    except ValueError:
                        pass

        # --- n8n Webhook ---

        if self.config.api.enable_webhooks:

            @app.post("/api/webhooks/n8n")
            async def n8n_webhook(req: WebhookRequest):
                try:
                    job_id = await self.job_manager.submit(
                        task=req.task,
                        context=req.context or {},
                        submitted_by="n8n",
                    )

                    # Broadcast event
                    await self._broadcast_event({
                        "type": "job.submitted",
                        "job_id": job_id,
                        "source": "n8n",
                        "task": req.task[:200],
                    })

                    return {
                        "job_id": job_id,
                        "status": "queued",
                        "message": "Job submitted via n8n webhook",
                    }
                except Exception as e:
                    logger.error(f"n8n webhook failed: {e}")
                    raise HTTPException(status_code=500, detail="Internal server error")

        # --- v2: Pi Agent Team endpoints ---

        from .modes import VALID_MODES as _VALID_MODES
        _modes_pattern = r"^(" + "|".join(_VALID_MODES) + r")$"

        class TeamRunRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            mode: Optional[str] = Field(default=None, pattern=_modes_pattern)
            skip_cost_check: bool = False
            user_context: Optional[Dict[str, Any]] = None

        class ApprovalRequest(BaseModel):
            choice: str = Field(..., pattern=r"^(approve|downgrade|reject)$")

        @app.get("/api/teams")
        async def list_teams():
            if not self.orchestrator or not hasattr(self.orchestrator, "team_registry"):
                return {"teams": [], "note": "Teams not initialized"}
            return self.orchestrator.team_registry.get_fleet_status()

        @app.get("/api/teams/{team_id}")
        async def get_team(team_id: str):
            if not self.orchestrator or not hasattr(self.orchestrator, "team_registry"):
                raise HTTPException(status_code=503, detail="Teams not initialized")
            config = self.orchestrator.team_registry.get_config(team_id)
            if not config:
                raise HTTPException(status_code=404, detail=f"Team not found: {team_id}")
            return {
                "id": config.id,
                "role": config.role,
                "mode": config.mode or "default",
                "enabled": config.enabled,
                "always_on": config.always_on,
                "lead_pi": config.lead_pi,
                "pi_count": len(config.pis),
                "pis": [{"id": p.id, "model": p.model} for p in config.pis],
                "active": team_id in self.orchestrator.team_registry.list_active(),
            }

        @app.post("/api/teams/{team_id}/run")
        async def run_team_task(team_id: str, req: TeamRunRequest):
            if not self.orchestrator:
                raise HTTPException(status_code=503, detail="Orchestrator not available")
            try:
                result = await self.orchestrator.run_team(
                    task=req.task,
                    team_id=team_id,
                    skip_cost_check=req.skip_cost_check,
                    mode=req.mode,
                    user_context=req.user_context,
                )
                # No manual broadcast  orchestrator emits TASK_COMPLETED via EventBus
                return {"team_id": team_id, "result": result}
            except OrchestratorError as e:
                raise HTTPException(status_code=400, detail=str(e))

        @app.get("/api/finance/summary")
        async def finance_summary():
            if not self.orchestrator or not hasattr(self.orchestrator, "cost_gate"):
                return {"note": "CostGate not initialized"}
            return self.orchestrator.cost_gate.get_spending_summary()

        @app.get("/api/finance/report")
        async def finance_report(hours: float = 24):
            if not self.orchestrator or not hasattr(self.orchestrator, "cost_gate"):
                return {"note": "CostGate not initialized"}
            return self.orchestrator.cost_gate.get_daily_report(hours=hours)

        @app.post("/api/finance/circuit-breaker/reset")
        async def reset_circuit_breaker():
            if not self.orchestrator or not hasattr(self.orchestrator, "cost_gate"):
                raise HTTPException(status_code=503, detail="CostGate not initialized")
            cg = self.orchestrator.cost_gate
            if cg._circuit_breaker:
                cg._circuit_breaker.reset()
                return {"status": "reset", "circuit_breaker": cg._circuit_breaker.get_status()}
            return {"status": "no_circuit_breaker"}

        @app.post("/api/finance/approve/{approval_id}")
        async def resolve_approval(approval_id: str, req: ApprovalRequest):
            if not self.orchestrator or not hasattr(self.orchestrator, "cost_gate"):
                raise HTTPException(status_code=503, detail="CostGate not initialized")
            resolved = self.orchestrator.cost_gate.resolve_approval(
                approval_id, req.choice
            )
            if not resolved:
                raise HTTPException(
                    status_code=404,
                    detail=f"No pending approval: {approval_id}",
                )
            return {"status": "resolved", "approval_id": approval_id, "choice": req.choice}

        @app.get("/api/scheduler/status")
        async def scheduler_status():
            if not self.orchestrator or not hasattr(self.orchestrator, "scheduler"):
                return {"note": "Scheduler not initialized"}
            return self.orchestrator.scheduler.get_status()

        @app.post("/api/scheduler/{task_name}/trigger")
        async def trigger_scheduled_task(task_name: str):
            if not self.orchestrator or not hasattr(self.orchestrator, "scheduler"):
                raise HTTPException(status_code=503, detail="Scheduler not initialized")
            ok = await self.orchestrator.scheduler.run_now(task_name)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"Scheduled task not found: {task_name}",
                )
            return {"status": "triggered", "task": task_name}

        @app.get("/api/schedules")
        async def list_schedules(user_id: Optional[str] = None):
            scheduler = getattr(self.orchestrator, "user_scheduler", None)
            if not self.orchestrator or scheduler is None:
                return {"schedules": [], "note": "UserScheduler not initialized"}
            jobs = await scheduler.list_jobs(user_id=user_id)
            return {
                "schedules": [j.to_dict() for j in jobs],
                "total": len(jobs),
                "status": scheduler.get_status(),
            }

        @app.delete("/api/schedules/{schedule_id}")
        async def cancel_schedule(schedule_id: str, user_id: Optional[str] = None):
            scheduler = getattr(self.orchestrator, "user_scheduler", None)
            if not self.orchestrator or scheduler is None:
                raise HTTPException(status_code=503, detail="UserScheduler not initialized")
            ok = await scheduler.cancel_job(schedule_id, user_id=user_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
            return {"status": "cancelled", "schedule_id": schedule_id}

        @app.get("/api/v2/status")
        async def v2_status():
            if not self.orchestrator:
                return {"note": "Orchestrator not available"}
            return self.orchestrator.get_teams_status()

        # --- Company Management ---

        class CompanyCreateRequest(BaseModel):
            id: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
            name: str = Field(..., min_length=1, max_length=200)
            domain: str = ""
            enabled: bool = True
            # manifest extension fields
            bindings: Optional[list] = None
            preferences: Optional[dict] = None
            ceo: Optional[dict] = None
            schedules: Optional[list] = None
            env: Optional[Dict[str, str]] = None
            shared_teams: Optional[List[str]] = None
            routing_bindings: Optional[list] = None
            memory_seed: Optional[dict] = None
            mcp_servers: Optional[list] = None
            # inline team definitions (external repos pass team file contents via API)
            teams: Optional[List[dict]] = None

        class CompanyUpdateRequest(BaseModel):
            name: Optional[str] = None
            domain: Optional[str] = None
            enabled: Optional[bool] = None
            ceo: Optional[Dict[str, Any]] = None
            preferences: Optional[Dict[str, Any]] = None
            schedules: Optional[list] = None
            env: Optional[Dict[str, str]] = None
            shared_teams: Optional[List[str]] = None
            routing_bindings: Optional[list] = None
            memory_seed: Optional[dict] = None
            mcp_servers: Optional[list] = None
            teams: Optional[List[dict]] = None

        class CompanyBindRequest(BaseModel):
            channel: Optional[str] = None
            chat_id: Optional[str] = None
            user_id: Optional[str] = None

        def _validate_inline_teams_payload(teams: Optional[List[dict]]) -> List[dict]:
            validated: List[dict] = []
            for team_def in teams or []:
                tid = team_def.get("id", "")
                if not tid:
                    continue
                if not _SAFE_ID_RE.match(tid):
                    raise HTTPException(status_code=400, detail=f"Invalid team ID: {tid!r}")
                for pi_def in team_def.get("pis", []):
                    pid = pi_def.get("id", "")
                    if not pid:
                        continue
                    if not _SAFE_ID_RE.match(pid):
                        raise HTTPException(status_code=400, detail=f"Invalid pi ID: {pid!r}")
                validated.append(team_def)
            return validated

        @app.get("/api/companies")
        async def list_companies():
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                return {"companies": [], "note": "Company registry not initialized"}
            registry = self.orchestrator.company_registry
            companies = []
            for cid in registry.list_companies():
                config = registry.get(cid)
                if config:
                    companies.append({
                        "id": config.id,
                        "name": config.name,
                        "domain": config.domain[:100] if config.domain else "",
                        "enabled": config.enabled,
                        "bindings_count": len(config.bindings),
                        "ceo_enabled": config.ceo.enabled,
                    })
            return {"companies": companies, "total": len(companies)}

        @app.post("/api/companies")
        async def create_company(req: CompanyCreateRequest):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            registry = self.orchestrator.company_registry
            if registry.get(req.id):
                raise HTTPException(status_code=409, detail=f"Company already exists: {req.id}")
            validated_teams = _validate_inline_teams_payload(req.teams)
            from .company import CompanyConfig
            # Build full config from manifest fields
            config_data = {"id": req.id, "name": req.name, "domain": req.domain, "enabled": req.enabled}
            for field_name in ("bindings", "preferences", "ceo", "schedules", "env",
                               "shared_teams", "routing_bindings", "memory_seed", "mcp_servers"):
                val = getattr(req, field_name, None)
                if val is not None:
                    config_data[field_name] = val
            config = CompanyConfig(**config_data)
            registry.save(config)

            # Write inline team definitions to disk
            if validated_teams:
                base = Path(self.orchestrator.memory.base_path)
                for team_def in validated_teams:
                    tid = team_def.get("id", "")
                    if not tid:
                        continue
                    team_dir = base / "companies" / req.id / "teams" / tid
                    team_dir.mkdir(parents=True, exist_ok=True)
                    # Write team.md
                    team_md = team_def.get("team_md", "")
                    if team_md:
                        (team_dir / "team.md").write_text(team_md, encoding="utf-8")
                    # Write pi soul.md files
                    for pi_def in team_def.get("pis", []):
                        pid = pi_def.get("id", "")
                        if not pid:
                            continue
                        pi_dir = team_dir / "pis" / pid
                        pi_dir.mkdir(parents=True, exist_ok=True)
                        soul_md = pi_def.get("soul_md", "")
                        if soul_md:
                            (pi_dir / "soul.md").write_text(soul_md, encoding="utf-8")

            # Immediate apply (no 30s watcher delay)
            await self.orchestrator.apply_company(req.id)
            return {"status": "created", "id": config.id}

        @app.get("/api/companies/{company_id}")
        async def get_company(company_id: str):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            config = self.orchestrator.company_registry.get(company_id)
            if not config:
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            data = config.model_dump()
            # Redact env vars (sensitive)
            data["env"] = {k: "***" for k in data.get("env", {})}
            # Enrich with runtime info
            if hasattr(self.orchestrator, "team_registry"):
                data["teams"] = list(
                    self.orchestrator.team_registry.get_configs_by_company(company_id).keys()
                )
            if hasattr(self.orchestrator, "scheduler"):
                sched_status = self.orchestrator.scheduler.get_status()
                data["schedule_status"] = {
                    name: info for name, info in sched_status.get("tasks", {}).items()
                    if name.startswith(f"company_{company_id}_") or name == f"ceo_{company_id}"
                }
            # Recent jobs
            recent = await self.job_manager.list_jobs(company_id=company_id, limit=10)
            data["recent_jobs"] = [j.to_dict() for j in recent]
            return data

        @app.patch("/api/companies/{company_id}")
        async def update_company(company_id: str, req: CompanyUpdateRequest):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            registry = self.orchestrator.company_registry
            config = registry.get(company_id)
            if not config:
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            validated_teams = _validate_inline_teams_payload(req.teams) if req.teams is not None else None
            update = req.model_dump(exclude_none=True)
            data = config.model_dump()
            # Deep merge for nested config objects
            for key, val in update.items():
                if key in ("ceo", "preferences") and isinstance(val, dict):
                    existing = data.get(key, {})
                    if isinstance(existing, dict):
                        existing.update(val)
                        data[key] = existing
                    else:
                        data[key] = val
                elif key == "bindings" and isinstance(val, list):
                    data[key] = val
                else:
                    data[key] = val
            from .company import CompanyConfig
            updated = CompanyConfig(**data)
            registry.save(updated)
            # Write inline team definitions if provided
            if validated_teams is not None:
                base = Path(self.orchestrator.memory.base_path)
                teams_root = base / "companies" / company_id / "teams"
                # Write inline team definitions
                provided_ids = set()
                for team_def in validated_teams:
                    tid = team_def.get("id", "")
                    if not tid:
                        continue
                    provided_ids.add(tid)
                    team_dir = teams_root / tid
                    team_dir.mkdir(parents=True, exist_ok=True)
                    team_md = team_def.get("team_md", "")
                    if team_md:
                        (team_dir / "team.md").write_text(team_md, encoding="utf-8")
                    for pi_def in team_def.get("pis", []):
                        pid = pi_def.get("id", "")
                        if not pid:
                            continue
                        pi_dir = team_dir / "pis" / pid
                        pi_dir.mkdir(parents=True, exist_ok=True)
                        soul_md = pi_def.get("soul_md", "")
                        if soul_md:
                            (pi_dir / "soul.md").write_text(soul_md, encoding="utf-8")
                # Remove team directories not in the updated manifest
                if teams_root.exists():
                    import shutil
                    for existing in teams_root.iterdir():
                        if existing.is_dir() and existing.name not in provided_ids:
                            if not _SAFE_ID_RE.match(existing.name):
                                logger.warning(f"Skipping removal of invalid team directory: {existing.name}")
                                continue
                            shutil.rmtree(existing)
                            logger.info(f"Removed stale team directory: {existing.name}")
            # Immediate apply
            await self.orchestrator.apply_company(company_id)
            return {"status": "updated", "id": company_id}

        @app.delete("/api/companies/{company_id}")
        async def delete_company(company_id: str):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            registry = self.orchestrator.company_registry
            if not registry.get(company_id):
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            await self.orchestrator.teardown_company(company_id)
            registry.delete(company_id)
            return {"status": "deleted", "id": company_id}

        @app.get("/api/companies/{company_id}/jobs")
        async def list_company_jobs(company_id: str, limit: int = 20, status: Optional[str] = None):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            if not self.orchestrator.company_registry.get(company_id):
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            filter_status = None
            if status:
                try:
                    filter_status = JobStatus(status)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
            jobs = await self.job_manager.list_jobs(
                status=filter_status, limit=limit, company_id=company_id,
            )
            return {"jobs": [j.to_dict() for j in jobs], "total": len(jobs)}

        @app.post("/api/companies/{company_id}/bind")
        async def add_company_binding(company_id: str, req: CompanyBindRequest):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            registry = self.orchestrator.company_registry
            config = registry.get(company_id)
            if not config:
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            from .company import CompanyBinding
            binding = CompanyBinding(channel=req.channel, chat_id=req.chat_id, user_id=req.user_id)
            data = config.model_dump()
            data["bindings"].append(binding.model_dump())
            from .company import CompanyConfig
            updated = CompanyConfig(**data)
            registry.save(updated)
            return {"status": "binding_added", "id": company_id, "bindings_count": len(updated.bindings)}

        # --- Global Bindings ---

        @app.get("/api/bindings")
        async def get_global_bindings():
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                return {"bindings": []}
            bindings = self.orchestrator.company_registry.get_global_bindings()
            return {"bindings": [b.model_dump() for b in bindings]}

        @app.put("/api/bindings")
        async def set_global_bindings(bindings: list):
            if not self.orchestrator or not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            from .company import GlobalBinding
            parsed = [GlobalBinding(**b) for b in bindings]
            self.orchestrator.company_registry.save_global_bindings(parsed)
            return {"status": "saved", "count": len(parsed)}

        # Mount NiceGUI admin UI (optional  requires nicegui package)
        if api_token:
            try:
                from .admin import init_admin
                init_admin(app, self.orchestrator, api_token)
                logger.info("Admin UI mounted at /admin")
            except ImportError:
                logger.info("NiceGUI not installed, admin UI disabled")

        self._app = app
        return app

    async def _on_event_bus(self, event) -> None:
        """EventBus subscriber  forward lifecycle events to WebSocket clients."""
        await self._broadcast_event({
            "type": event.type.value,
            **event.data,
        })

    async def _broadcast_event(self, event: dict) -> None:
        """Broadcast an event to all WebSocket subscribers."""
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        for queue in self._event_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def start(self) -> None:
        """Start the API server using uvicorn."""
        try:
            import uvicorn
        except ImportError:
            raise ImportError("uvicorn required. Install with: pip install uvicorn")

        app = self.create_app()

        config = uvicorn.Config(
            app,
            host=self.config.api.host,
            port=self.config.api.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()

    def get_app(self):
        """Get the FastAPI app instance (for external ASGI servers)."""
        if not self._app:
            self.create_app()
        return self._app
