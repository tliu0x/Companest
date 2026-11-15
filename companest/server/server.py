import logging
import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone

from ..utils import run_sync
from ..config import CompanestConfig
from .event_store import EventStore
from ..jobs.jobs import JobManager, JobStatus
from ..orchestration.orchestrator import CompanestOrchestrator
from ..companies.company import _SAFE_ID_RE, CompanyBinding, CompanyConfig, GlobalBinding
from ..exceptions import JobError, OrchestratorError
from ..runtime.substrate import SubstrateError

logger = logging.getLogger(__name__)


class CompanestAPIServer:
    def __init__(
        self,
        config: CompanestConfig,
        job_manager: JobManager,
        orchestrator: Optional[CompanestOrchestrator] = None,
    ):
        self.config = config
        self.job_manager = job_manager
        self.orchestrator = orchestrator
        self._event_subscribers: List[asyncio.Queue] = []
        self.MAX_WS_SUBSCRIBERS = 50
        self._event_store: Optional[EventStore] = None
        self._event_history_enabled = False
        self._event_sequence = 0
        self._event_sequence_lock = asyncio.Lock()

    async def _extract_event_company_id(self, event: Dict[str, Any]) -> Optional[str]:
        company_id = event.get("company_id")
        if isinstance(company_id, str) and company_id:
            return company_id
        for key in ("team_id", "target_team"):
            team_id = event.get(key)
            if isinstance(team_id, str) and "/" in team_id:
                return team_id.split("/", 1)[0]
        job_id = event.get("job_id")
        if isinstance(job_id, str) and job_id:
            job = self.job_manager._jobs.get(job_id)
            if job and job.company_id:
                return job.company_id
        return None

    def _build_company_summary(
        self,
        company_id: str,
        config: CompanyConfig,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "id": config.id,
            "name": config.name,
            "domain": config.domain[:100],
            "enabled": config.enabled,
            "bindings_count": len(config.bindings),
            "ceo_enabled": config.ceo.enabled,
            "active_team_count": len(self._company_teams(company_id)),
            "recent_job_count": snapshot["recent_job_count"],
            "last_activity_timestamp": snapshot["last_activity_timestamp"],
        }

    def _company_teams(self, company_id: str) -> List[str]:
        registry = getattr(self.orchestrator, "team_registry", None)
        return list(registry.get_configs_by_company(company_id)) if registry is not None else []

    def _get_api_capabilities(self) -> Dict[str, bool]:
        return {
            "admin": bool(self.config.api.auth_token),
            "companies": hasattr(self.orchestrator, "company_registry"),
            "finance": hasattr(self.orchestrator, "cost_gate"),
            "scheduler": hasattr(self.orchestrator, "scheduler"),
            "user_scheduler": bool(getattr(self.orchestrator, "user_scheduler", None)),
            "websocket": self.config.api.enable_websocket_events,
            "events_history": self._event_history_enabled,
            "public_knowledge": os.environ.get("ENABLE_PUBLIC_KNOWLEDGE_V1", "").lower() in ("1", "true"),
        }

    def create_app(self):
        try:
            from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
            from fastapi.middleware.cors import CORSMiddleware
            from pydantic import BaseModel, Field
        except ImportError:
            raise ImportError(
                "FastAPI required. Install with: pip install fastapi uvicorn"
            )

        @asynccontextmanager
        async def lifespan(application):
            owns_jobs = bool(self.job_manager.settings and not self.job_manager._db)
            if owns_jobs:
                await self.job_manager.start()
            self._event_store = EventStore(self.job_manager._data_dir)
            self._event_history_enabled = await self._event_store.start()
            if self._event_history_enabled:
                self._event_sequence = await self._event_store.get_latest_sequence()
                logger.info("Event history initialized at sequence %s", self._event_sequence)
            else:
                self._event_store = None
                self._event_sequence = 0
            if hasattr(self.orchestrator, "scheduler"):
                await self.orchestrator.scheduler.start()
                logger.info("Scheduler started via lifespan")
            if self.orchestrator:
                self.orchestrator.events.on_any(self._on_event_bus)
                logger.info("Server subscribed to EventBus")
            yield
            if self.orchestrator:
                self.orchestrator.events.off_any(self._on_event_bus)
            if hasattr(self.orchestrator, "scheduler"):
                await self.orchestrator.scheduler.stop()
                logger.info("Scheduler stopped via lifespan")
            from ..knowledge.feeds import close_client as close_feed_client
            await close_feed_client()
            if self._event_store:
                await self._event_store.close()
            self._event_store = None
            self._event_history_enabled = False
            if owns_jobs:
                await self.job_manager.stop()

        app = FastAPI(
            title="Companest Control Panel",
            description="Companest Fleet Management API",
            version="1.0.0",
            lifespan=lifespan,
        )

        if self.config.api.allowed_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=self.config.api.allowed_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

        api_token = self.config.api.auth_token
        if api_token:
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            class AuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    if request.method == "OPTIONS":
                        return await call_next(request)
                    # /ws/ is exempt here: the WebSocket handler checks the token in its handshake.
                    if (request.url.path == "/health" or request.url.path.startswith("/ws/")
                            or request.url.path.startswith("/internal/jobs/")):
                        return await call_next(request)
                    auth_header = request.headers.get("Authorization", "")
                    token = auth_header.removeprefix("Bearer ").strip()
                    if token != api_token:
                        return JSONResponse(
                            {"detail": "Unauthorized"},
                            status_code=401,
                        )
                    return await call_next(request)

            app.add_middleware(AuthMiddleware)
        else:
            is_debug = self.config.debug or os.environ.get("COMPANEST_DEBUG", "").lower() in ("1", "true")
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

        class SubmitJobRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            context: Optional[Dict[str, Any]] = None
            submitted_by: str = Field(default="api", max_length=100)
            company_id: Optional[str] = Field(default=None, max_length=100)
            idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)

        class WebhookRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            context: Optional[Dict[str, Any]] = None
            callback_url: Optional[str] = Field(default=None, max_length=2000)

        class AuthorizationRequest(BaseModel):
            request_id: str = Field(max_length=128)
            task: str = Field(min_length=1, max_length=50000)
            team_id: str = Field(min_length=1, max_length=200)
            model: str = Field(max_length=200)
            priority: str = "normal"

        def actor_job(request, job_id):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            try:
                return self.job_manager.authenticate_actor(job_id, token)
            except JobError:
                raise HTTPException(status_code=403, detail="Invalid or inactive actor capability")

        @app.post("/internal/jobs/{job_id}/authorize")
        async def authorize_actor(job_id: str, req: AuthorizationRequest, request: Request):
            job = actor_job(request, job_id)
            try:
                return await self.job_manager.authorize(job, req.model_dump())
            except JobError as exc:
                raise HTTPException(status_code=403, detail=str(exc))

        @app.post("/internal/jobs/{job_id}/schedule")
        async def schedule_actor(job_id: str, request: Request):
            job = actor_job(request, job_id)
            scheduler = getattr(self.orchestrator, "user_scheduler", None)
            user_id = job.context.get("user_id")
            if not scheduler or not user_id:
                raise HTTPException(status_code=400, detail="Scheduler and submitting user identity are required")
            body = await request.json()
            action = body.get("action")
            try:
                if action == "add":
                    team_id = body.get("team_id")
                    if team_id and not self.orchestrator.can_access_team(job.company_id, team_id):
                        raise JobError("Team is outside this job's company scope")
                    scheduled = await scheduler.add_job(
                        user_id=user_id, chat_id=job.context.get("chat_id", ""),
                        channel=job.context.get("channel", "api"), company_id=job.company_id,
                        workspace_id=job.context.get("workspace_id"),
                        task=body["task"], description=body.get("description", ""),
                        trigger_type=body["trigger_type"], trigger_args=body["trigger_args"],
                        team_id=team_id, mode=body.get("mode", "default"),
                    )
                    return scheduled.to_dict()
                jobs = await scheduler.list_jobs(user_id=user_id)
                owned = [j for j in jobs if j.company_id == job.company_id]
                if action == "list":
                    return [j.to_dict() for j in owned]
                if action == "cancel":
                    target = body.get("job_id")
                    if not any(j.id == target for j in owned):
                        raise JobError("Schedule is outside this job's scope")
                    return await scheduler.cancel_job(target, user_id=user_id)
                raise JobError("Unknown schedule action")
            except (KeyError, ValueError, JobError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @app.post("/internal/jobs/{job_id}/events")
        async def actor_event(job_id: str, request: Request):
            job = actor_job(request, job_id)
            body = await request.json()
            if body.get("type") not in {"task_started", "task_completed", "task_failed", "task_routed"}:
                raise HTTPException(status_code=400, detail="Unsupported actor business event")
            await self._broadcast_event({
                **body.get("data", {}), "type": body["type"],
                "job_id": job.id, "company_id": job.company_id,
            })
            return {"accepted": True}

        def _parse_status(status: Optional[str]) -> Optional[JobStatus]:
            if not status:
                return None
            try:
                return JobStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        @app.get("/health")
        async def health_check():
            return {
                "status": "ok",
                "service": "companest-control-panel",
                "version": "1.0.0",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        @app.get("/api/meta")
        async def api_meta():
            return {
                "service": "companest-control-panel",
                "version": "1.0.0",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "capabilities": self._get_api_capabilities(),
            }

        @app.post("/api/jobs")
        async def submit_job(req: SubmitJobRequest):
            try:
                job_id = await self.job_manager.submit(
                    task=req.task,
                    context=req.context,
                    submitted_by=req.submitted_by,
                    company_id=req.company_id,
                    idempotency_key=req.idempotency_key,
                )
                await self._broadcast_event({
                    "type": "job.submitted",
                    "job_id": job_id,
                    "company_id": req.company_id,
                    "submitted_by": req.submitted_by,
                    "task": req.task[:200],
                })
                job = self.job_manager._jobs[job_id]
                return {"job_id": job_id, "status": job.status.value}
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
            company_id: Optional[str] = None,
            limit: int = 50,
            offset: int = 0,
        ):
            filter_status = _parse_status(status)
            total_matching = await self.job_manager.count_jobs(
                status=filter_status, company_id=company_id,
            )
            jobs = await self.job_manager.list_jobs(
                status=filter_status, company_id=company_id,
                limit=limit, offset=offset,
            )
            result: Dict[str, Any] = {
                "jobs": [j.to_dict() for j in jobs],
                "total": total_matching,
                "limit": limit,
                "offset": offset,
                "projection": "last_observed; query an individual job to refresh execution",
            }
            if not filter_status and not company_id:
                result["stats"] = self.job_manager.get_stats()
            return result

        @app.post("/api/jobs/{job_id}/cancel")
        async def cancel_job(job_id: str):
            try:
                cancelled = await self.job_manager.cancel_job(job_id)
                if cancelled:
                    await self._broadcast_event({
                        "type": "job.cancelled",
                        "job_id": job_id,
                    })
                    return {"status": "cancelled", "job_id": job_id}
                return {"status": "not_cancellable", "job_id": job_id}
            except JobError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except SubstrateError as e:
                raise HTTPException(status_code=503, detail=str(e))

        @app.get("/api/fleet/status")
        async def fleet_status():
            status = {
                "jobs": self.job_manager.get_stats(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if hasattr(self.orchestrator, "team_registry"):
                status["teams"] = self.orchestrator.team_registry.get_fleet_status()
            if hasattr(self.orchestrator, "company_registry"):
                registry = self.orchestrator.company_registry
                company_ids = registry.list_companies()
                snapshot = self.job_manager.get_company_activity_snapshot(company_ids)
                companies = {}
                for cid in company_ids:
                    cfg = registry.get(cid)
                    companies[cid] = {
                        "name": cfg.name if cfg else cid,
                        "enabled": cfg.enabled if cfg else False,
                        "active_teams": len(self._company_teams(cid)),
                        "total_jobs": snapshot[cid]["total_jobs"],
                    }
                status["companies"] = companies
            return status

        if self.config.api.enable_websocket_events:

            @app.websocket("/ws/events")
            async def websocket_events(websocket: WebSocket):
                await websocket.accept()
                try:
                    handshake = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
                except Exception:
                    await websocket.close(code=4003, reason="Handshake required")
                    return
                if not isinstance(handshake, dict) or handshake.get("type") != "auth":
                    await websocket.close(code=4003, reason="Handshake required")
                    return

                token = str(handshake.get("token", "") or "")
                auth_required = bool(api_token)
                if auth_required and token != api_token:
                    await websocket.close(code=4001, reason="Unauthorized")
                    return
                if len(self._event_subscribers) >= self.MAX_WS_SUBSCRIBERS:
                    await websocket.close(code=4002, reason="Too many connections")
                    return

                last_sequence_id = handshake.get("last_sequence_id")
                if not isinstance(last_sequence_id, int):
                    last_sequence_id = None

                queue: asyncio.Queue = asyncio.Queue(maxsize=100)
                self._event_subscribers.append(queue)
                current_sequence = self._event_sequence
                await websocket.send_json({
                    "type": "ready",
                    "sequence_id": current_sequence,
                    "missed_events": (
                        last_sequence_id is not None and last_sequence_id < current_sequence
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                queue.get(), timeout=30.0
                            )
                            await websocket.send_json(event)
                        except asyncio.TimeoutError:
                            await websocket.send_json({
                                "type": "ping",
                                "sequence_id": self._event_sequence,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug(f"WebSocket error: {e}")
                finally:
                    self._event_subscribers.remove(queue)

        if self.config.api.enable_webhooks:

            @app.post("/api/webhooks/n8n")
            async def n8n_webhook(req: WebhookRequest):
                try:
                    job_id = await self.job_manager.submit(
                        task=req.task,
                        context=req.context,
                        submitted_by="n8n",
                    )
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

        from ..modes import VALID_MODES as _VALID_MODES
        _modes_pattern = r"^(" + "|".join(_VALID_MODES) + r")$"

        class TeamRunRequest(BaseModel):
            task: str = Field(..., min_length=1, max_length=50000)
            mode: Optional[str] = Field(default=None, pattern=_modes_pattern)
            cascade: Optional[bool] = None
            workflow: Optional[str] = Field(default=None, pattern=r"^(decompose|delegate)$")
            skip_cost_check: bool = False
            user_context: Optional[Dict[str, Any]] = None

        class ApprovalRequest(BaseModel):
            choice: str = Field(..., pattern=r"^(approve|downgrade|reject)$")

        @app.get("/api/events")
        async def list_events(
            event_type: Optional[str] = None,
            company_id: Optional[str] = None,
            hours: Optional[float] = None,
            start: Optional[str] = None,
            end: Optional[str] = None,
            limit: int = 50,
            offset: int = 0,
        ):
            if limit < 1 or limit > 500:
                raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
            if offset < 0:
                raise HTTPException(status_code=400, detail="offset must be >= 0")
            if hours is not None and hours <= 0:
                raise HTTPException(status_code=400, detail="hours must be > 0")
            if hours is not None and start is None:
                start_dt = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours)
                start = start_dt.isoformat()

            if not self._event_history_enabled:
                return {
                    "events": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "latest_sequence_id": self._event_sequence,
                    "note": "Event history not initialized",
                }

            events, total = await self._event_store.list_events(
                event_type=event_type,
                company_id=company_id,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )
            return {
                "events": [event.to_dict() for event in events],
                "total": total,
                "limit": limit,
                "offset": offset,
                "latest_sequence_id": self._event_sequence,
            }

        @app.get("/api/teams")
        async def list_teams():
            if not hasattr(self.orchestrator, "team_registry"):
                return {
                    "registered": [],
                    "active": [],
                    "configs": {},
                    "note": "Teams not initialized",
                }
            return self.orchestrator.team_registry.get_fleet_status()

        @app.get("/api/teams/{team_id:path}")
        async def get_team(team_id: str):
            if not hasattr(self.orchestrator, "team_registry"):
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

        @app.post("/api/teams/{team_id:path}/run")
        async def run_team_task(team_id: str, req: TeamRunRequest):
            if not self.orchestrator:
                raise HTTPException(status_code=503, detail="Orchestrator not available")
            try:
                result = await self.orchestrator.run_team(
                    task=req.task,
                    team_id=team_id,
                    skip_cost_check=req.skip_cost_check,
                    mode=req.mode,
                    user_context={**(req.user_context or {}),
                                  **({"cascade": req.cascade} if req.cascade is not None else {}),
                                  **({"workflow": req.workflow} if req.workflow else {})},
                )
                return {"team_id": team_id, "result": result}
            except (OrchestratorError, JobError) as e:
                raise HTTPException(status_code=400, detail=str(e))

        @app.get("/api/finance/summary")
        async def finance_summary():
            if not hasattr(self.orchestrator, "cost_gate"):
                return {
                    "total": 0.0,
                    "by_team": {},
                    "entries": 0,
                    "days": 0,
                    "today": 0.0,
                    "window_spend": 0.0,
                    "budget": {
                        "daily_limit": 0.0,
                        "mode": "disabled",
                        "rolling_window_hours": 24,
                        "team_budgets": {},
                        "overflow_pool": 0.0,
                    },
                    "source": "unavailable",
                    "mode": "disabled",
                    "circuit_breaker": None,
                    "pending_approvals": [],
                    "pending_approvals_count": 0,
                    "note": "CostGate not initialized",
                }
            return await run_sync(self.orchestrator.cost_gate.get_spending_summary)

        @app.get("/api/finance/report")
        async def finance_report(hours: float = 24):
            if not hasattr(self.orchestrator, "cost_gate"):
                return {
                    "window_hours": hours,
                    "window_spend": 0.0,
                    "daily_limit": 0.0,
                    "utilization_pct": 0.0,
                    "by_team": {},
                    "team_utilization": {},
                    "mode": "disabled",
                    "circuit_breaker": None,
                    "overflow_pool": 0.0,
                    "overflow_used": 0.0,
                    "pending_approvals_count": 0,
                    "note": "CostGate not initialized",
                }
            return await run_sync(self.orchestrator.cost_gate.get_daily_report, hours=hours)

        @app.post("/api/finance/circuit-breaker/reset")
        async def reset_circuit_breaker():
            if not hasattr(self.orchestrator, "cost_gate"):
                raise HTTPException(status_code=503, detail="CostGate not initialized")
            cg = self.orchestrator.cost_gate
            if cg._circuit_breaker:
                cg._circuit_breaker.reset()
                return {"status": "reset", "circuit_breaker": cg._circuit_breaker.get_status()}
            return {"status": "no_circuit_breaker"}

        @app.post("/api/finance/approve/{approval_id}")
        async def resolve_approval(approval_id: str, req: ApprovalRequest):
            if not hasattr(self.orchestrator, "cost_gate"):
                raise HTTPException(status_code=503, detail="CostGate not initialized")
            resolved = self.orchestrator.cost_gate.resolve_approval(
                approval_id, req.choice
            )
            if not resolved:
                raise HTTPException(
                    status_code=404,
                    detail=f"No pending approval: {approval_id}",
                )
            await self._broadcast_event({
                "type": "finance.approval_resolved",
                "approval_id": approval_id,
                "choice": req.choice,
            })
            return {"status": "resolved", "approval_id": approval_id, "choice": req.choice}

        @app.get("/api/scheduler/status")
        async def scheduler_status():
            if not hasattr(self.orchestrator, "scheduler"):
                return {"started": False, "tasks": {}, "note": "Scheduler not initialized"}
            return self.orchestrator.scheduler.get_status()

        @app.post("/api/scheduler/{task_name}/trigger")
        async def trigger_scheduled_task(task_name: str):
            if not hasattr(self.orchestrator, "scheduler"):
                raise HTTPException(status_code=503, detail="Scheduler not initialized")
            ok = await self.orchestrator.scheduler.run_now(task_name)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"Scheduled task not found: {task_name}",
                )
            await self._broadcast_event({
                "type": "scheduler.task_triggered",
                "task_name": task_name,
            })
            return {"status": "triggered", "task": task_name}

        @app.get("/api/schedules")
        async def list_schedules(user_id: Optional[str] = None):
            scheduler = getattr(self.orchestrator, "user_scheduler", None)
            if scheduler is None:
                return {
                    "schedules": [],
                    "total": 0,
                    "status": {"started": False, "db_path": "", "active_jobs": 0, "next_run": None},
                    "note": "UserScheduler not initialized",
                }
            jobs = await scheduler.list_jobs(user_id=user_id)
            return {
                "schedules": [j.to_dict() for j in jobs],
                "total": len(jobs),
                "status": scheduler.get_status(),
            }

        @app.delete("/api/schedules/{schedule_id}")
        async def cancel_schedule(schedule_id: str, user_id: Optional[str] = None):
            scheduler = getattr(self.orchestrator, "user_scheduler", None)
            if scheduler is None:
                raise HTTPException(status_code=503, detail="UserScheduler not initialized")
            ok = await scheduler.cancel_job(schedule_id, user_id=user_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Schedule not found: {schedule_id}")
            await self._broadcast_event({
                "type": "schedule.cancelled",
                "schedule_id": schedule_id,
                "user_id": user_id,
            })
            return {"status": "cancelled", "schedule_id": schedule_id}

        @app.get("/api/v2/status")
        async def v2_status():
            if not self.orchestrator:
                return {"note": "Orchestrator not available"}
            return await self.orchestrator.get_teams_status_async()

        class CompanyCreateRequest(BaseModel):
            id: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
            name: str = Field(..., min_length=1, max_length=200)
            domain: str = ""
            enabled: bool = True
            bindings: Optional[list] = None
            preferences: Optional[dict] = None
            ceo: Optional[dict] = None
            schedules: Optional[list] = None
            env: Optional[Dict[str, str]] = None
            shared_teams: Optional[List[str]] = None
            routing_bindings: Optional[list] = None
            memory_seed: Optional[dict] = None
            mcp_servers: Optional[list] = None
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

        def _company_registry():
            if not hasattr(self.orchestrator, "company_registry"):
                raise HTTPException(status_code=503, detail="Company registry not initialized")
            return self.orchestrator.company_registry

        def _require_company(company_id: str):
            registry = _company_registry()
            config = registry.get(company_id)
            if not config:
                raise HTTPException(status_code=404, detail=f"Company not found: {company_id}")
            return registry, config

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

        def _company_mutation(handler):
            @wraps(handler)
            async def wrapped(*args, **kwargs):
                _company_registry()
                return await self.orchestrator.company_operation(handler, *args, **kwargs)
            return wrapped

        def _write_inline_teams(company_id: str, validated_teams: List[dict], replace=False) -> Path:
            """Write validated team definitions to disk and return the company's teams directory."""
            teams_root = Path(self.orchestrator.memory.base_path) / "companies" / company_id / "teams"
            for team_def in validated_teams:
                team_dir = teams_root / team_def["id"]
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
            if replace:
                provided_ids = {team_def["id"] for team_def in validated_teams}
                if teams_root.exists():
                    for existing in teams_root.iterdir():
                        if existing.is_dir() and existing.name not in provided_ids:
                            if not _SAFE_ID_RE.match(existing.name):
                                logger.warning(f"Skipping removal of invalid team directory: {existing.name}")
                                continue
                            shutil.rmtree(existing)
                            logger.info(f"Removed stale team directory: {existing.name}")
            return teams_root

        @app.get("/api/companies")
        async def list_companies():
            if not hasattr(self.orchestrator, "company_registry"):
                return {"companies": [], "total": 0, "note": "Company registry not initialized"}
            registry = self.orchestrator.company_registry
            company_ids = registry.list_companies()
            snapshot = self.job_manager.get_company_activity_snapshot(company_ids)
            companies = [
                self._build_company_summary(cid, registry.get(cid), snapshot[cid])
                for cid in company_ids
            ]
            return {"companies": companies, "total": len(companies)}

        @app.post("/api/companies")
        @_company_mutation
        async def create_company(req: CompanyCreateRequest):
            registry = _company_registry()
            if registry.get(req.id):
                raise HTTPException(status_code=409, detail=f"Company already exists: {req.id}")
            validated_teams = _validate_inline_teams_payload(req.teams)
            config_data = {"id": req.id, "name": req.name, "domain": req.domain, "enabled": req.enabled}
            for field_name in ("bindings", "preferences", "ceo", "schedules", "env",
                               "shared_teams", "routing_bindings", "memory_seed", "mcp_servers"):
                val = getattr(req, field_name)
                if val is not None:
                    config_data[field_name] = val
            config = CompanyConfig(**config_data)
            await registry.save_async(config)
            await run_sync(_write_inline_teams, req.id, validated_teams)
            await self.orchestrator.apply_company(req.id)
            await self._broadcast_event({
                "type": "company.created",
                "company_id": req.id,
                "name": req.name,
            })
            return {"status": "created", "id": config.id}

        @app.get("/api/companies/{company_id}")
        async def get_company(company_id: str):
            _, config = _require_company(company_id)
            data = config.model_dump()
            data["env"] = {k: "***" for k in data["env"]}
            if hasattr(self.orchestrator, "team_registry"):
                data["teams"] = self._company_teams(company_id)
            if hasattr(self.orchestrator, "scheduler"):
                sched_status = self.orchestrator.scheduler.get_status()
                data["schedule_status"] = {
                    name: info for name, info in sched_status["tasks"].items()
                    if name.startswith(f"company_{company_id}_") or name == f"ceo_{company_id}"
                }
            recent = await self.job_manager.list_jobs(company_id=company_id, limit=10)
            data["recent_jobs"] = [j.to_dict() for j in recent]
            data["summary"] = self._build_company_summary(
                company_id,
                config,
                self.job_manager.get_company_activity_snapshot([company_id])[company_id],
            )
            if hasattr(self.orchestrator, "cost_gate"):
                cost_gate = self.orchestrator.cost_gate
                data["finance"] = {
                    "hourly_budget_usd": config.preferences.budget_hourly_usd,
                    "monthly_budget_usd": config.preferences.budget_monthly_usd,
                    "last_hour_spend": round(await run_sync(cost_gate._get_company_window_spending, company_id, 1.0), 4),
                    "last_24h_spend": round(await run_sync(cost_gate._get_company_window_spending, company_id, 24.0), 4),
                }
            return data

        @app.patch("/api/companies/{company_id}")
        @_company_mutation
        async def update_company(company_id: str, req: CompanyUpdateRequest):
            registry, config = _require_company(company_id)
            validated_teams = _validate_inline_teams_payload(req.teams) if req.teams is not None else None
            update = req.model_dump(exclude_none=True)
            data = config.model_dump()
            for key, val in update.items():
                if key in ("ceo", "preferences"):
                    data[key].update(val)
                else:
                    data[key] = val
            await registry.save_async(CompanyConfig(**data))
            if validated_teams is not None:
                await run_sync(_write_inline_teams, company_id, validated_teams, replace=True)
            await self.orchestrator.apply_company(company_id)
            await self._broadcast_event({
                "type": "company.updated",
                "company_id": company_id,
                "fields": sorted(update.keys()),
            })
            return {"status": "updated", "id": company_id}

        @app.delete("/api/companies/{company_id}")
        @_company_mutation
        async def delete_company(company_id: str):
            registry, _ = _require_company(company_id)
            await self.orchestrator.teardown_company(company_id)
            await registry.delete_async(company_id)
            await self._broadcast_event({
                "type": "company.deleted",
                "company_id": company_id,
            })
            return {"status": "deleted", "id": company_id}

        @app.get("/api/companies/{company_id}/jobs")
        async def list_company_jobs(company_id: str, limit: int = 20, status: Optional[str] = None):
            _require_company(company_id)
            filter_status = _parse_status(status)
            jobs = await self.job_manager.list_jobs(
                status=filter_status, limit=limit, company_id=company_id,
            )
            total_matching = await self.job_manager.count_jobs(
                status=filter_status, company_id=company_id,
            )
            return {"jobs": [j.to_dict() for j in jobs], "total": total_matching}

        @app.post("/api/companies/{company_id}/bind")
        @_company_mutation
        async def add_company_binding(company_id: str, req: CompanyBindRequest):
            registry, config = _require_company(company_id)
            binding = CompanyBinding(channel=req.channel, chat_id=req.chat_id, user_id=req.user_id)
            data = config.model_dump()
            data["bindings"].append(binding.model_dump())
            updated = CompanyConfig(**data)
            await registry.save_async(updated)
            await self._broadcast_event({
                "type": "company.binding_added",
                "company_id": company_id,
                "binding": binding.model_dump(),
            })
            return {"status": "binding_added", "id": company_id, "bindings_count": len(updated.bindings)}

        @app.get("/api/bindings")
        async def get_global_bindings():
            if not hasattr(self.orchestrator, "company_registry"):
                return {"bindings": []}
            bindings = self.orchestrator.company_registry.get_global_bindings()
            return {"bindings": [b.model_dump() for b in bindings]}

        @app.put("/api/bindings")
        @_company_mutation
        async def set_global_bindings(bindings: List[GlobalBinding]):
            registry = _company_registry()
            await registry.save_global_bindings_async(bindings)
            return {"status": "saved", "count": len(bindings)}

        if os.environ.get("ENABLE_PUBLIC_KNOWLEDGE_V1", "").lower() in ("1", "true"):
            from ..knowledge.public import create_public_knowledge_router
            app.include_router(create_public_knowledge_router())
            logger.info("Public Knowledge API enabled")

            from ..knowledge.digests import DigestS3Store, create_digest_router
            app.include_router(create_digest_router(DigestS3Store(
                bucket=os.environ.get("DIGEST_S3_BUCKET", ""),
                prefix=os.environ.get("DIGEST_S3_PREFIX", "companest-digests/"),
                region=os.environ.get("DIGEST_S3_REGION", "us-east-1"),
                endpoint_url=os.environ.get("DIGEST_S3_ENDPOINT_URL") or None,
            )))
            logger.info("Digest API enabled")

        return app

    async def _on_event_bus(self, event) -> None:
        await self._broadcast_event({
            "type": event.type.value,
            "timestamp": event.timestamp,
            **event.data,
        })

    async def _broadcast_event(self, event: dict) -> None:
        """Persist an event (when history is enabled) and fan it out to WebSocket subscribers."""
        payload = dict(event)
        event_type = str(payload.pop("type", "event"))
        timestamp = payload.pop("timestamp", None) or datetime.now(timezone.utc).isoformat()
        company_id = await self._extract_event_company_id(payload)

        async with self._event_sequence_lock:
            if self._event_history_enabled:
                stored_event = await self._event_store.append_event(
                    event_type=event_type,
                    payload=payload,
                    timestamp=timestamp,
                    company_id=company_id,
                )
                self._event_sequence = stored_event.sequence_id
                outbound = stored_event.to_dict()
            else:
                self._event_sequence += 1
                outbound = {
                    "sequence_id": self._event_sequence,
                    "type": event_type,
                    "timestamp": timestamp,
                    "company_id": company_id,
                    "payload": payload,
                }

            # Fan out under the lock so subscribers see sequence ids in order.
            for queue in self._event_subscribers:
                try:
                    queue.put_nowait(dict(outbound))
                except asyncio.QueueFull:
                    pass

    async def start(self) -> None:
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
