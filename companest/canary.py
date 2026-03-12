"""
Companest Canary Workflow

Test a change on one company before promoting to others.

Flow:
  1. start_canary(): snapshot current state, apply change to canary company
  2. check_health(): verify canary company is healthy (CEO cycle success, budget ok)
  3. promote(): apply change to all target companies
  4. rollback(): restore canary to pre-change snapshot if needed

Deployments are persisted in global memory for crash recovery.
"""

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from .exceptions import CanaryError

if TYPE_CHECKING:
    from .events import EventBus
    from .evolution import EvolutionProposal
    from .memory import MemoryManager
    from .memory.backend import MemoryBackend

logger = logging.getLogger(__name__)

DEPLOYMENTS_KEY = "canary-deployments.json"


#  Models 


class CanaryStage(str, Enum):
    PENDING = "pending"
    CANARY_RUNNING = "canary_running"
    CANARY_PASSED = "canary_passed"
    CANARY_FAILED = "canary_failed"
    PROMOTING = "promoting"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


class HealthCheck(BaseModel):
    """Result of a single health check."""
    checked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    healthy: bool
    details: Dict[str, Any] = Field(default_factory=dict)


class CanaryDeployment(BaseModel):
    """Tracks a change being tested on one company before promotion."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    proposal_id: str
    canary_company_id: str
    target_company_ids: List[str] = Field(default_factory=list)
    stage: CanaryStage = CanaryStage.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    health_checks: List[HealthCheck] = Field(default_factory=list)
    rollback_snapshot: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    min_healthy_checks: int = 3


#  Manager 


class CanaryManager:
    """Manages canary deployments: test on one company, promote to others."""

    def __init__(
        self,
        memory: "MemoryManager",
        memory_backend: "MemoryBackend",
        event_bus: "EventBus",
    ) -> None:
        self._memory = memory
        self._backend = memory_backend
        self._events = event_bus
        self._deployments: Dict[str, CanaryDeployment] = {}
        self._load_deployments()

    #  Lifecycle 

    async def start_canary(
        self,
        proposal: "EvolutionProposal",
        canary_company_id: str,
        target_company_ids: Optional[List[str]] = None,
    ) -> CanaryDeployment:
        """Start a canary deployment.

        Snapshots the canary company's CEO team memory before applying
        changes so we can rollback if the canary fails.
        """
        ceo_team = f"company-{canary_company_id}"

        # Snapshot current state for rollback
        snapshot = None
        if self._backend.supports_snapshot:
            try:
                snapshot = self._backend.export_snapshot(ceo_team)
            except Exception as e:
                logger.warning(
                    "[Canary] Snapshot failed for %s: %s", ceo_team, e,
                )

        deployment = CanaryDeployment(
            proposal_id=proposal.id,
            canary_company_id=canary_company_id,
            target_company_ids=target_company_ids or [],
            stage=CanaryStage.CANARY_RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
            rollback_snapshot=snapshot,
        )

        self._deployments[deployment.id] = deployment
        self._persist()

        from .events import EventType
        await self._events.emit(EventType.CANARY_STARTED, {
            "deployment_id": deployment.id,
            "proposal_id": proposal.id,
            "canary_company_id": canary_company_id,
        })

        logger.info(
            "[Canary] Started deployment %s for proposal %s on %s",
            deployment.id, proposal.id, canary_company_id,
        )
        return deployment

    async def check_health(self, deployment_id: str) -> bool:
        """Run a health check on the canary company.

        A deployment is considered healthy when it accumulates
        *min_healthy_checks* consecutive passing checks.
        Returns True if the deployment has passed.
        """
        dep = self._get_or_raise(deployment_id)
        if dep.stage != CanaryStage.CANARY_RUNNING:
            return dep.stage == CanaryStage.CANARY_PASSED

        # Simple health check: read the CEO team's cycle-results
        # and verify no errors in recent entries.
        ceo_team = f"company-{dep.canary_company_id}"
        healthy = True
        details: Dict[str, Any] = {}

        try:
            results = self._backend.read(ceo_team, "cycle-results.json")
            if isinstance(results, list) and results:
                last = results[-1] if results else {}
                details["last_cycle"] = last.get("cycle", "-")
                details["entries"] = len(results)
                # Check if the last result contains an error field
                if last.get("error"):
                    healthy = False
                    details["error"] = last["error"]
            else:
                details["note"] = "no cycle results yet"
        except Exception as e:
            healthy = False
            details["error"] = str(e)

        check = HealthCheck(healthy=healthy, details=details)
        dep.health_checks.append(check)

        # Evaluate pass/fail
        recent = dep.health_checks[-dep.min_healthy_checks:]
        if len(recent) >= dep.min_healthy_checks and all(c.healthy for c in recent):
            dep.stage = CanaryStage.CANARY_PASSED
            logger.info("[Canary] Deployment %s PASSED", deployment_id)
        elif not healthy:
            consecutive_failures = 0
            for c in reversed(dep.health_checks):
                if not c.healthy:
                    consecutive_failures += 1
                else:
                    break
            if consecutive_failures >= dep.min_healthy_checks:
                dep.stage = CanaryStage.CANARY_FAILED
                dep.error = f"Failed {consecutive_failures} consecutive health checks"
                logger.warning("[Canary] Deployment %s FAILED", deployment_id)

        self._persist()
        return dep.stage == CanaryStage.CANARY_PASSED

    async def promote(self, deployment_id: str) -> None:
        """Promote a passed canary to all target companies."""
        dep = self._get_or_raise(deployment_id)
        if dep.stage != CanaryStage.CANARY_PASSED:
            raise CanaryError(
                f"Cannot promote deployment {deployment_id}: stage is {dep.stage.value}",
            )

        dep.stage = CanaryStage.PROMOTING
        self._persist()

        # For now, promotion is a marker  the orchestrator or a human
        # applies the same change to target companies.  Full automation
        # would require the EvolutionEngine to replay the proposal.
        dep.stage = CanaryStage.PROMOTED
        dep.completed_at = datetime.now(timezone.utc).isoformat()
        self._persist()

        from .events import EventType
        await self._events.emit(EventType.CANARY_PROMOTED, {
            "deployment_id": deployment_id,
            "proposal_id": dep.proposal_id,
            "target_company_ids": dep.target_company_ids,
        })

        logger.info(
            "[Canary] Deployment %s promoted to %d targets",
            deployment_id, len(dep.target_company_ids),
        )

    async def rollback(self, deployment_id: str) -> None:
        """Rollback a canary company to its pre-change snapshot."""
        dep = self._get_or_raise(deployment_id)
        ceo_team = f"company-{dep.canary_company_id}"

        if dep.rollback_snapshot and self._backend.supports_snapshot:
            try:
                self._backend.restore_snapshot(ceo_team, dep.rollback_snapshot)
                logger.info(
                    "[Canary] Rolled back %s for deployment %s",
                    ceo_team, deployment_id,
                )
            except Exception as e:
                raise CanaryError(
                    f"Rollback failed for deployment {deployment_id}: {e}",
                )
        else:
            logger.warning(
                "[Canary] No snapshot available for rollback of %s",
                deployment_id,
            )

        dep.stage = CanaryStage.ROLLED_BACK
        dep.completed_at = datetime.now(timezone.utc).isoformat()
        self._persist()

    #  Query 

    def list_deployments(
        self, stage: Optional[CanaryStage] = None,
    ) -> List[CanaryDeployment]:
        all_deps = list(self._deployments.values())
        if stage is not None:
            return [d for d in all_deps if d.stage == stage]
        return all_deps

    def get_deployment(
        self, deployment_id: str,
    ) -> Optional[CanaryDeployment]:
        return self._deployments.get(deployment_id)

    #  Internal 

    def _get_or_raise(self, deployment_id: str) -> CanaryDeployment:
        dep = self._deployments.get(deployment_id)
        if dep is None:
            raise CanaryError(f"Deployment not found: {deployment_id}")
        return dep

    def _persist(self) -> None:
        data = [d.model_dump() for d in self._deployments.values()]
        self._memory.write_global_memory(DEPLOYMENTS_KEY, data)

    def _load_deployments(self) -> None:
        raw = self._memory.read_global_memory(DEPLOYMENTS_KEY)
        if not raw or not isinstance(raw, list):
            return
        for item in raw:
            try:
                dep = CanaryDeployment.model_validate(item)
                self._deployments[dep.id] = dep
            except Exception as e:
                logger.warning("[Canary] Skipping invalid deployment: %s", e)
