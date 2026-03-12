"""Tests for CanaryManager lifecycle, health checks, and rollback."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from companest.canary import CanaryDeployment, CanaryManager, CanaryStage
from companest.events import EventBus
from companest.evolution import EvolutionProposal, Observation, SourceType
from companest.memory.backend import FileBackend
from companest.memory.manager import MemoryManager


@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "memory").mkdir()
        (Path(td) / "teams" / "company-canary" / "memory").mkdir(parents=True)
        yield MemoryManager(td)


@pytest.fixture
def backend(memory):
    return FileBackend(memory)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def manager(memory, backend, event_bus):
    return CanaryManager(memory, backend, event_bus)


@pytest.fixture
def proposal():
    return EvolutionProposal(
        title="Upgrade pydantic",
        description="Upgrade to pydantic 2.10",
        observations=[Observation(
            source_name="pypi",
            source_type=SourceType.PYPI_VERSION,
            title="pydantic 2.10",
            summary="New version",
        )],
    )


#  Lifecycle tests 


@pytest.mark.asyncio
async def test_start_canary(manager, proposal):
    dep = await manager.start_canary(
        proposal, "canary", target_company_ids=["prod1", "prod2"],
    )
    assert dep.stage == CanaryStage.CANARY_RUNNING
    assert dep.canary_company_id == "canary"
    assert dep.target_company_ids == ["prod1", "prod2"]
    assert dep.proposal_id == proposal.id
    assert dep.started_at is not None


@pytest.mark.asyncio
async def test_start_canary_with_snapshot(manager, proposal, memory, backend):
    # Write some data to snapshot
    memory.write_team_memory("company-canary", "state.json", {"version": 1})

    dep = await manager.start_canary(proposal, "canary")
    assert dep.rollback_snapshot is not None
    assert "entries" in dep.rollback_snapshot


#  Health check tests 


@pytest.mark.asyncio
async def test_health_check_no_data(manager, proposal):
    dep = await manager.start_canary(proposal, "canary")
    result = await manager.check_health(dep.id)
    assert result is False  # not enough checks yet
    assert len(dep.health_checks) == 1
    assert dep.health_checks[0].healthy is True


@pytest.mark.asyncio
async def test_health_check_passes_after_min_checks(manager, proposal):
    dep = await manager.start_canary(proposal, "canary")

    for _ in range(dep.min_healthy_checks):
        result = await manager.check_health(dep.id)

    assert result is True
    assert dep.stage == CanaryStage.CANARY_PASSED


@pytest.mark.asyncio
async def test_health_check_fails_on_errors(manager, proposal, memory):
    dep = await manager.start_canary(proposal, "canary")

    # Write error cycle results
    memory.write_team_memory("company-canary", "cycle-results.json", [
        {"cycle": 1, "error": "budget exceeded"},
    ])

    for _ in range(dep.min_healthy_checks):
        await manager.check_health(dep.id)

    assert dep.stage == CanaryStage.CANARY_FAILED


#  Promote tests 


@pytest.mark.asyncio
async def test_promote_after_pass(manager, proposal):
    dep = await manager.start_canary(proposal, "canary", ["prod1"])

    # Pass health checks
    for _ in range(dep.min_healthy_checks):
        await manager.check_health(dep.id)

    assert dep.stage == CanaryStage.CANARY_PASSED
    await manager.promote(dep.id)
    assert dep.stage == CanaryStage.PROMOTED
    assert dep.completed_at is not None


@pytest.mark.asyncio
async def test_promote_rejects_non_passed(manager, proposal):
    from companest.exceptions import CanaryError

    dep = await manager.start_canary(proposal, "canary")
    with pytest.raises(CanaryError):
        await manager.promote(dep.id)


#  Rollback tests 


@pytest.mark.asyncio
async def test_rollback_restores_snapshot(manager, proposal, memory):
    # Set up initial state
    memory.write_team_memory("company-canary", "state.json", {"version": 1})

    dep = await manager.start_canary(proposal, "canary")

    # Simulate a change
    memory.write_team_memory("company-canary", "state.json", {"version": 2})
    assert memory.read_team_memory("company-canary", "state.json") == {"version": 2}

    # Rollback
    await manager.rollback(dep.id)
    assert dep.stage == CanaryStage.ROLLED_BACK

    # Data restored
    restored = memory.read_team_memory("company-canary", "state.json")
    assert restored == {"version": 1}


#  Query tests 


@pytest.mark.asyncio
async def test_list_deployments(manager, proposal):
    dep1 = await manager.start_canary(proposal, "canary1")
    dep2 = await manager.start_canary(proposal, "canary2")

    all_deps = manager.list_deployments()
    assert len(all_deps) == 2

    running = manager.list_deployments(stage=CanaryStage.CANARY_RUNNING)
    assert len(running) == 2


@pytest.mark.asyncio
async def test_get_deployment(manager, proposal):
    dep = await manager.start_canary(proposal, "canary")
    found = manager.get_deployment(dep.id)
    assert found is not None
    assert found.id == dep.id
    assert manager.get_deployment("nonexistent") is None


#  Persistence tests 


@pytest.mark.asyncio
async def test_deployments_persist_across_instances(manager, proposal, memory, backend, event_bus):
    dep = await manager.start_canary(proposal, "canary")

    # Create a new manager instance  should load persisted deployments
    manager2 = CanaryManager(memory, backend, event_bus)
    found = manager2.get_deployment(dep.id)
    assert found is not None
    assert found.canary_company_id == "canary"
