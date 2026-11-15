"""Agent business orchestration executed exclusively inside a Substrate actor."""

from typing import Any, Dict, Optional, Tuple
from ..orchestration.orchestrator import CompanestOrchestrator, logger
from ..orchestration.router import RoutingDecision, TeamAssignment
from ..orchestration.multi_team import run_multi_team
from ..exceptions import OrchestratorError, TeamError
from ..events import EventType
from ..agents.pi import _run_tool_context
from ..modes import ProgressCallback, normalize_execution
from ..orchestration.decomposition import Decomposition
from ..orchestration.delegation import Delegation
from ..utils import run_sync


class ActorOrchestrator(CompanestOrchestrator):
    def __init__(self, config, *, job_context):
        super().__init__(config)
        self._job_context = dict(job_context)

    def _bound_context(self, context):
        context = dict(context or {})
        # Nested CEO/delegation/refresh calls keep the identity and the one
        # workspace locked by ActorWorkload. Omitted fields never widen scope.
        for key in ("company_id", "workspace_id", "user_id", "chat_id", "channel"):
            expected = self._job_context.get(key)
            if key in context and context[key] != expected:
                raise OrchestratorError(f"Nested task cannot change {key}")
            if key in self._job_context or key == "company_id":
                context[key] = expected
        return context

    async def run_team(
        self,
        task: str,
        team_id: str,
        skip_cost_check: bool = False,
        mode: Optional[str] = None,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
    ) -> str:
        if not hasattr(self, "team_registry"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

        user_context = self._bound_context(user_context)
        company_id = user_context.get("company_id")
        company_budget_hourly = None
        if company_id and not company_budget_hourly:
            company = self.company_registry.get(company_id)
            if company:
                company_budget_hourly = company.preferences.budget_hourly_usd

        # Company isolation: private teams are only reachable by their owner
        if not self.can_access_team(company_id, team_id):
            raise OrchestratorError(
                f"Access denied: company '{company_id}' cannot access team '{team_id}'"
            )

        try:
            team = self.team_registry.create(team_id)
        except TeamError as e:
            raise OrchestratorError(f"Team routing failed: {e}")
        effective_mode, cascade, workflow = normalize_execution(
            mode if mode is not None else team.mode,
            user_context.get("cascade", getattr(team, "cascade", False)),
            user_context.get("workflow", getattr(team, "workflow", None) if mode is None else None),
        )

        lead_config = team.get_lead_config()
        model = lead_config.model if lead_config else "deepseek-chat"

        decision = await self.cost_gate.evaluate(
            task, team_id, model, priority=priority,
            company_id=company_id,
            company_budget_hourly=company_budget_hourly,
        )

        if decision.action == "rejected":
            raise OrchestratorError(
                f"CostGate rejected: {decision.reason}",
                details={
                    "team": team_id,
                    "model": model,
                    "estimated_cost": decision.estimate.estimated_cost_usd,
                },
            )

        if decision.action in ("auto_approve", "notify_approve"):
            logger.info(
                f"[CostGate] {decision.action}: "
                f"${decision.estimate.estimated_cost_usd:.4f} for {team_id}"
            )
            if on_progress:
                await on_progress(
                    f"\U0001f4b0 ${decision.estimate.estimated_cost_usd:.4f} approved"
                )

        extra_context: Dict[str, Any] = {}
        workspace_id = user_context.get("workspace_id")
        ws = self.workspace_registry.get(workspace_id) if workspace_id else None
        if ws:
            extra_context["workspace_path"] = ws.path
            extra_context["workspace_context"] = await run_sync(self.workspace_registry.build_context, workspace_id)
        extra_context["company_shared_teams"] = self._get_company_shared_teams(company_id)
        for pi in team.pis.values():
            pi.user_scheduler = self.user_scheduler
            if team_id == f"company-{company_id}":
                company = self.company_registry.get(company_id)
                pi._extra_tool_context = {
                    "run_team_fn": self.run_team, "run_auto_fn": self.run_auto,
                    "company_id": company_id, "company_env": company.env,
                    "company_shared_teams": company.shared_teams,
                }

        event_data = {"team_id": team_id, "mode": effective_mode, "task_preview": task[:200]}
        if company_id:
            event_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_STARTED, event_data)

        tool_context_token = _run_tool_context.set(extra_context)
        try:
            try:
                import copy
                execution_mode = copy.copy(self.mode_registry.get(effective_mode))
                execution_mode._cascade = cascade
                if workflow == "decompose":
                    execution_mode = Decomposition(cascade=cascade)
                elif workflow == "delegate":
                    if user_context.get("delegation_depth", 0) >= 1:
                        raise OrchestratorError("Nested delegation is not allowed")
                    user_context = {**user_context, "delegation_depth": 1}
                    allowed = [tid for tid in self.team_registry.list_teams() if self.can_access_team(company_id, tid)]
                    execution_mode = Delegation(self.run_team, allowed_teams=allowed)
            except KeyError:
                raise OrchestratorError(
                    f"Unknown execution mode: '{effective_mode}'",
                    details={"available": self.mode_registry.list_modes()},
                )
            result = await execution_mode.execute(
                team, task, on_progress=on_progress, user_context=user_context,
            )
        except OrchestratorError:
            raise
        except Exception as e:
            await self.events.emit(EventType.TASK_FAILED, {
                "team_id": team_id, "mode": effective_mode, "error": str(e),
            })
            raise OrchestratorError(
                f"Team '{team_id}' execution failed: {e}",
                details={"team": team_id, "mode": effective_mode},
            )
        finally:
            _run_tool_context.reset(tool_context_token)

        completed_data = {"team_id": team_id, "mode": effective_mode}
        if company_id:
            completed_data["company_id"] = company_id
        await self.events.emit(EventType.TASK_COMPLETED, completed_data)

        return result

    async def run_auto(
        self,
        task: str,
        mode: Optional[str] = None,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, RoutingDecision]:
        if not hasattr(self, "_smart_router"):
            raise OrchestratorError(
                "Teams not initialized. Call init_teams() first."
            )

        user_context = self._bound_context(user_context)
        preferred_teams = None
        company_id = user_context.get("company_id")
        if preferred_teams is None and company_id:
            company = self.company_registry.get(company_id)
            if company and company.preferences.preferred_teams:
                preferred_teams = company.preferences.preferred_teams

        decision = await self._smart_router.route(
            task, preferred_teams=preferred_teams,
            company_id=company_id,
            shared_teams=self._get_company_shared_teams(company_id),
        )

        if decision.declined:
            raise OrchestratorError(
                f"No suitable team found: {decision.decline_reason or 'task does not match any team'}",
                details={"declined": True, "reasoning": decision.reasoning},
            )

        effective_mode = mode if mode is not None else decision.mode
        user_context.setdefault("cascade", getattr(decision, "cascade", False))
        if getattr(decision, "workflow", None):
            user_context.setdefault("workflow", decision.workflow)

        if on_progress:
            teams_str = "+".join(t.team_id for t in decision.teams)
            await on_progress(
                f"\U0001f50d \u2192 {teams_str} ({effective_mode}, {decision.confidence:.0%})"
            )

        routed_ids = {t.team_id for t in decision.teams}
        if self._info_refresh_team not in routed_ids:
            if on_progress:
                await on_progress("\U0001f4e1 Refreshing info feeds...")
            await self.background.refresh_info_for_task(task, user_context=user_context)

        if len(decision.teams) == 1:
            assignment = decision.teams[0]
            result = await self.run_team(
                assignment.instruction or task,
                assignment.team_id,
                mode=effective_mode,
                on_progress=on_progress,
                user_context=user_context,
                priority=decision.task_priority,
            )
            return result, decision

        result = await run_multi_team(
            self.run_team, decision, mode=effective_mode,
            on_progress=on_progress, user_context=user_context,
        )
        return result, decision

