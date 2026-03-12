"""Tests for EvolutionEngine, source schemas, and proposal generation."""

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from companest.evolution import (
    EvolutionEngine,
    EvolutionProposal,
    Observation,
    ObservationSource,
    ProposalStatus,
    SourceTier,
    SourceType,
)
from companest.events import EventBus
from companest.memory.manager import MemoryManager


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "memory").mkdir()
        yield MemoryManager(td)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def engine(memory, event_bus):
    return EvolutionEngine(memory, event_bus)


#  Source schema tests 


def test_source_tiers():
    assert ObservationSource(type=SourceType.GITHUB_RELEASE, name="x").tier == SourceTier.A
    assert ObservationSource(type=SourceType.CVE_OSV, name="x").tier == SourceTier.A
    assert ObservationSource(type=SourceType.PYPI_VERSION, name="x").tier == SourceTier.B
    assert ObservationSource(type=SourceType.MODEL_CHANGELOG, name="x").tier == SourceTier.B
    assert ObservationSource(type=SourceType.COMMUNITY, name="x").tier == SourceTier.C


def test_observation_tier():
    obs = Observation(
        source_name="test",
        source_type=SourceType.GITHUB_ADVISORY,
        title="CVE-2025-1234",
        summary="Critical vulnerability",
        severity="critical",
    )
    assert obs.tier == SourceTier.A


#  Engine registration tests 


def test_register_source(engine):
    src = ObservationSource(type=SourceType.COMMUNITY, name="hn")
    engine.register_source(src)
    assert "hn" in engine._sources


def test_register_fetcher(engine):
    async def dummy_fetcher(source):
        return []

    engine.register_fetcher(SourceType.COMMUNITY, dummy_fetcher)
    assert SourceType.COMMUNITY in engine._fetchers


#  Observation check tests 


@pytest.mark.asyncio
async def test_check_sources_no_sources(engine):
    results = await engine.check_sources()
    assert results == []


@pytest.mark.asyncio
async def test_check_sources_with_fetcher(engine):
    src = ObservationSource(type=SourceType.COMMUNITY, name="test")
    engine.register_source(src)

    async def fetcher(source):
        return [Observation(
            source_name=source.name,
            source_type=source.type,
            title="New trend",
            summary="Something interesting",
        )]

    engine.register_fetcher(SourceType.COMMUNITY, fetcher)

    results = await engine.check_sources()
    assert len(results) == 1
    assert results[0].title == "New trend"
    assert src.last_checked is not None


@pytest.mark.asyncio
async def test_check_sources_fetcher_error(engine):
    src = ObservationSource(type=SourceType.COMMUNITY, name="failing")
    engine.register_source(src)

    async def bad_fetcher(source):
        raise RuntimeError("network error")

    engine.register_fetcher(SourceType.COMMUNITY, bad_fetcher)

    results = await engine.check_sources()
    assert results == []  # error is swallowed, logged


#  Proposal generation tests 


@pytest.mark.asyncio
async def test_generate_proposals_empty(engine):
    proposals = await engine.generate_proposals([])
    assert proposals == []


@pytest.mark.asyncio
async def test_generate_proposals_tier_a(engine):
    obs = Observation(
        source_name="sdk",
        source_type=SourceType.GITHUB_RELEASE,
        title="claude-agent-sdk v2.0",
        summary="Breaking change in API",
    )
    proposals = await engine.generate_proposals([obs])
    assert len(proposals) == 1
    assert proposals[0].change_type == "dependency_update"
    assert len(proposals[0].observations) == 1


@pytest.mark.asyncio
async def test_generate_proposals_tier_b_grouped(engine):
    obs1 = Observation(
        source_name="pypi1",
        source_type=SourceType.PYPI_VERSION,
        title="pydantic 2.10",
        summary="New version",
    )
    obs2 = Observation(
        source_name="pypi2",
        source_type=SourceType.PYPI_VERSION,
        title="httpx 0.28",
        summary="New version",
    )
    proposals = await engine.generate_proposals([obs1, obs2])
    assert len(proposals) == 1  # grouped into one
    assert len(proposals[0].observations) == 2
    assert "2" in proposals[0].title


@pytest.mark.asyncio
async def test_generate_proposals_tier_c_batched(engine):
    obs1 = Observation(
        source_name="hn",
        source_type=SourceType.COMMUNITY,
        title="AI agents in prod",
        summary="Discussion",
    )
    obs2 = Observation(
        source_name="reddit",
        source_type=SourceType.COMMUNITY,
        title="New prompting technique",
        summary="Discussion",
    )
    proposals = await engine.generate_proposals([obs1, obs2])
    assert len(proposals) == 1
    assert proposals[0].priority == "low"


@pytest.mark.asyncio
async def test_generate_proposals_security_high_priority(engine):
    obs = Observation(
        source_name="osv",
        source_type=SourceType.CVE_OSV,
        title="CVE-2025-9999",
        summary="Remote code execution",
        severity="critical",
    )
    proposals = await engine.generate_proposals([obs])
    assert proposals[0].priority == "high"
    assert proposals[0].change_type == "security_patch"


#  Proposal persistence tests 


def test_list_proposals_empty(engine):
    assert engine.list_proposals() == []


@pytest.mark.asyncio
async def test_run_cycle_persists(engine):
    src = ObservationSource(type=SourceType.COMMUNITY, name="test")
    engine.register_source(src)

    async def fetcher(source):
        return [Observation(
            source_name="test",
            source_type=SourceType.COMMUNITY,
            title="Signal",
            summary="Details",
        )]

    engine.register_fetcher(SourceType.COMMUNITY, fetcher)

    proposals = await engine.run_cycle()
    assert len(proposals) == 1

    stored = engine.list_proposals()
    assert len(stored) == 1
    assert stored[0].title == proposals[0].title


def test_update_proposal_status(engine):
    # Manually persist a proposal
    p = EvolutionProposal(
        title="Test",
        description="Test proposal",
        observations=[],
    )
    engine._persist_proposals([p])

    engine.update_proposal_status(p.id, ProposalStatus.APPROVED)
    updated = engine.get_proposal(p.id)
    assert updated.status == ProposalStatus.APPROVED


def test_update_proposal_status_not_found(engine):
    from companest.exceptions import EvolutionError
    with pytest.raises(EvolutionError):
        engine.update_proposal_status("nonexistent", ProposalStatus.APPROVED)
