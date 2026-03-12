"""
Companest Evolution Engine

Multi-source observation engine that generates structured proposals
for framework and company evolution.  Does NOT auto-modify code.

Signal flow:
  Sources -> check_sources() -> Observations -> generate_proposals() -> Proposals
  Proposals stored in memory for human or CEO review.

Source tiers:
  A (auto-proposal):  GitHub releases, Dependabot/CVE/OSV, model changelogs
  B (backlog item):    PyPI versions, perf/cost data
  C (candidate only):  HN, Reddit, research papers
"""

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from .exceptions import EvolutionError

if TYPE_CHECKING:
    from .events import EventBus, EventType
    from .memory import MemoryManager

logger = logging.getLogger(__name__)

PROPOSALS_KEY = "evolution-proposals.json"


#  Enums 


class SourceType(str, Enum):
    """Categories of observation sources, ordered by trust level."""
    GITHUB_RELEASE = "github_release"
    GITHUB_ADVISORY = "github_advisory"
    CVE_OSV = "cve_osv"
    PYPI_VERSION = "pypi_version"
    MODEL_CHANGELOG = "model_changelog"
    COMMUNITY = "community"


class SourceTier(str, Enum):
    """Trust tier determines what a source can automatically trigger."""
    A = "A"   # Can auto-generate fix proposals
    B = "B"   # Can generate backlog items
    C = "C"   # Can generate candidate experiments only


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    PROMOTED = "promoted"


# Mapping from source type to tier
SOURCE_TIER: Dict[SourceType, SourceTier] = {
    SourceType.GITHUB_RELEASE: SourceTier.A,
    SourceType.GITHUB_ADVISORY: SourceTier.A,
    SourceType.CVE_OSV: SourceTier.A,
    SourceType.PYPI_VERSION: SourceTier.B,
    SourceType.MODEL_CHANGELOG: SourceTier.B,
    SourceType.COMMUNITY: SourceTier.C,
}


#  Models 


class ObservationSource(BaseModel):
    """A single source to monitor for changes."""
    type: SourceType
    name: str
    url: Optional[str] = None
    check_interval: int = 3600
    last_checked: Optional[str] = None
    enabled: bool = True

    @property
    def tier(self) -> SourceTier:
        return SOURCE_TIER.get(self.type, SourceTier.C)


class Observation(BaseModel):
    """A detected change from a source."""
    source_name: str
    source_type: SourceType
    title: str
    summary: str
    url: Optional[str] = None
    severity: str = "info"
    detected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    raw_data: Dict[str, Any] = Field(default_factory=dict)

    @property
    def tier(self) -> SourceTier:
        return SOURCE_TIER.get(self.source_type, SourceTier.C)


class EvolutionProposal(BaseModel):
    """A proposed change generated from one or more observations."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    description: str
    observations: List[Observation]
    affected_files: List[str] = Field(default_factory=list)
    change_type: str = "general"
    priority: str = "normal"
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    canary_company_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Fetcher signature: async (source) -> List[Observation]
ObservationFetcher = Callable[
    ["ObservationSource"],
    Coroutine[Any, Any, List[Observation]],
]


#  Engine 


class EvolutionEngine:
    """
    Multi-source observation engine.

    Checks registered sources, collects observations, and generates
    structured proposals.  Proposals are stored in global memory
    for human or CEO review  the engine never modifies code itself.
    """

    def __init__(
        self,
        memory: "MemoryManager",
        event_bus: "EventBus",
    ) -> None:
        self._memory = memory
        self._events = event_bus
        self._sources: Dict[str, ObservationSource] = {}
        self._fetchers: Dict[SourceType, ObservationFetcher] = {}

    #  Registration 

    def register_source(self, source: ObservationSource) -> None:
        """Register a source to monitor."""
        self._sources[source.name] = source
        logger.info(
            "[Evolution] Registered source: %s (%s, tier %s)",
            source.name, source.type.value, source.tier.value,
        )

    def register_fetcher(
        self, source_type: SourceType, fn: ObservationFetcher,
    ) -> None:
        """Register a fetcher function for a source type."""
        self._fetchers[source_type] = fn

    #  Observation 

    async def check_sources(self) -> List[Observation]:
        """Run all enabled source checks, return new observations."""
        observations: List[Observation] = []
        now = datetime.now(timezone.utc).isoformat()

        for source in self._sources.values():
            if not source.enabled:
                continue
            fetcher = self._fetchers.get(source.type)
            if fetcher is None:
                logger.debug(
                    "[Evolution] No fetcher for source type %s, skipping %s",
                    source.type.value, source.name,
                )
                continue
            try:
                results = await fetcher(source)
                observations.extend(results)
                source.last_checked = now
            except Exception as e:
                logger.warning(
                    "[Evolution] Fetcher failed for %s: %s", source.name, e,
                )

        logger.info(
            "[Evolution] Checked %d sources, got %d observations",
            len(self._sources), len(observations),
        )
        return observations

    #  Proposal generation 

    async def generate_proposals(
        self, observations: List[Observation],
    ) -> List[EvolutionProposal]:
        """Analyze observations and generate proposals.

        Grouping rules:
        - Tier A observations each get their own proposal.
        - Tier B observations are batched into one proposal per source type.
        - Tier C observations are batched into a single "candidate" proposal.
        """
        if not observations:
            return []

        proposals: List[EvolutionProposal] = []

        # Tier A: one proposal per observation
        tier_a = [o for o in observations if o.tier == SourceTier.A]
        for obs in tier_a:
            proposals.append(EvolutionProposal(
                title=obs.title,
                description=obs.summary,
                observations=[obs],
                change_type=self._infer_change_type(obs),
                priority="high" if obs.severity in ("high", "critical") else "normal",
            ))

        # Tier B: group by source_type
        tier_b = [o for o in observations if o.tier == SourceTier.B]
        by_type: Dict[SourceType, List[Observation]] = {}
        for obs in tier_b:
            by_type.setdefault(obs.source_type, []).append(obs)
        for stype, group in by_type.items():
            titles = [o.title for o in group]
            proposals.append(EvolutionProposal(
                title=f"Backlog: {stype.value} updates ({len(group)})",
                description="\n".join(f"- {t}" for t in titles),
                observations=group,
                change_type="dependency_update",
                priority="normal",
            ))

        # Tier C: single batch
        tier_c = [o for o in observations if o.tier == SourceTier.C]
        if tier_c:
            titles = [o.title for o in tier_c]
            proposals.append(EvolutionProposal(
                title=f"Candidates: community signals ({len(tier_c)})",
                description="\n".join(f"- {t}" for t in titles),
                observations=tier_c,
                change_type="exploration",
                priority="low",
            ))

        return proposals

    #  Full cycle 

    async def run_cycle(self) -> List[EvolutionProposal]:
        """Full cycle: check sources -> generate proposals -> persist."""
        observations = await self.check_sources()
        proposals = await self.generate_proposals(observations)

        if proposals:
            self._persist_proposals(proposals)
            from .events import EventType
            for p in proposals:
                await self._events.emit(EventType.EVOLUTION_PROPOSAL_CREATED, {
                    "proposal_id": p.id,
                    "title": p.title,
                    "priority": p.priority,
                })

        return proposals

    #  Proposal management 

    def list_proposals(
        self, status: Optional[ProposalStatus] = None,
    ) -> List[EvolutionProposal]:
        """List stored proposals, optionally filtered by status."""
        all_proposals = self._load_proposals()
        if status is not None:
            return [p for p in all_proposals if p.status == status]
        return all_proposals

    def get_proposal(self, proposal_id: str) -> Optional[EvolutionProposal]:
        for p in self._load_proposals():
            if p.id == proposal_id:
                return p
        return None

    def update_proposal_status(
        self, proposal_id: str, status: ProposalStatus,
    ) -> None:
        """Update a proposal's status and persist."""
        proposals = self._load_proposals()
        for p in proposals:
            if p.id == proposal_id:
                p.status = status
                break
        else:
            raise EvolutionError(f"Proposal not found: {proposal_id}")
        self._save_proposals(proposals)

    #  Persistence 

    def _load_proposals(self) -> List[EvolutionProposal]:
        raw = self._memory.read_global_memory(PROPOSALS_KEY)
        if not raw or not isinstance(raw, list):
            return []
        return [EvolutionProposal.model_validate(item) for item in raw]

    def _save_proposals(self, proposals: List[EvolutionProposal]) -> None:
        self._memory.write_global_memory(
            PROPOSALS_KEY,
            [p.model_dump() for p in proposals],
        )

    def _persist_proposals(self, new: List[EvolutionProposal]) -> None:
        existing = self._load_proposals()
        existing.extend(new)
        self._save_proposals(existing)

    @staticmethod
    def _infer_change_type(obs: Observation) -> str:
        st = obs.source_type
        if st in (SourceType.CVE_OSV, SourceType.GITHUB_ADVISORY):
            return "security_patch"
        if st == SourceType.GITHUB_RELEASE:
            return "dependency_update"
        if st == SourceType.MODEL_CHANGELOG:
            return "model_swap"
        return "general"
