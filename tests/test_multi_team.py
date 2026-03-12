"""Tests for multi-team memory-task-hint propagation."""

import pytest

from companest.multi_team import run_multi_team
from companest.router import RoutingDecision, TeamAssignment


@pytest.mark.asyncio
async def test_sequential_multi_team_uses_original_assignment_for_memory_hint():
    calls = []

    async def run_team_fn(task, team_id, **kwargs):
        calls.append({
            "team_id": team_id,
            "task": task,
            "user_context": kwargs.get("user_context", {}),
        })
        return f"{team_id} result"

    decision = RoutingDecision(
        teams=[
            TeamAssignment(team_id="alpha", instruction="research launch", priority=1),
            TeamAssignment(team_id="beta", instruction="summarize launch", priority=2),
        ],
        strategy="sequential",
    )

    result = await run_multi_team(
        run_team_fn,
        decision,
        user_context={"company_id": "acme"},
    )

    assert result == "beta result"
    assert calls[0]["user_context"]["memory_task_hint"] == "research launch"
    assert "Context from previous teams" in calls[1]["task"]
    assert calls[1]["user_context"]["memory_task_hint"] == "summarize launch"


@pytest.mark.asyncio
async def test_parallel_multi_team_uses_assignment_for_memory_hint():
    seen_hints = []

    async def run_team_fn(task, team_id, **kwargs):
        seen_hints.append(kwargs.get("user_context", {}).get("memory_task_hint"))
        return f"{team_id} result"

    decision = RoutingDecision(
        teams=[
            TeamAssignment(team_id="alpha", instruction="research launch", priority=1),
            TeamAssignment(team_id="beta", instruction="summarize launch", priority=2),
        ],
        strategy="parallel",
    )

    result = await run_multi_team(run_team_fn, decision)

    assert "## alpha" in result
    assert "## beta" in result
    assert set(seen_hints) == {"research launch", "summarize launch"}
