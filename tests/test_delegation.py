"""Tests for Delegation -lead Pi evaluates and branches."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from companest.orchestration.delegation import Delegation
from companest.agents.team import AgentTeam, TeamConfig
from companest.agents.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import TeamError


@pytest.fixture
def team(tmp_path) -> AgentTeam:
    (tmp_path / "teams" / "test" / "memory").mkdir(parents=True)
    (tmp_path / "teams" / "test" / "pis" / "lead").mkdir(parents=True)
    (tmp_path / "teams" / "test" / "pis" / "lead" / "soul.md").write_text("Pi lead")
    (tmp_path / "soul.md").write_text("soul")
    (tmp_path / "user.md").write_text("user")
    config = TeamConfig(id="test", role="general", lead_pi="lead", pis=[PiConfig(id="lead")])
    return AgentTeam(config, MemoryManager(str(tmp_path)))


def _execute(mode, team, task, **kwargs):
    return asyncio.get_event_loop().run_until_complete(mode.execute(team, task, **kwargs))


def test_single_step_done(team):
    team.pis["lead"].run = AsyncMock(
        return_value='{"done": true, "summary": "Task is trivial, answer is 42"}'
    )
    run_team_fn = AsyncMock()

    result = _execute(Delegation(run_team_fn=run_team_fn), team, "What is 6*7-")
    assert "42" in result
    run_team_fn.assert_not_called()


def test_single_step_route_passes_memory_task_hints(team):
    team.pis["lead"].run = AsyncMock(side_effect=[
        '{"team": "research", "mode": "default", "sub_task": "investigate launch plan", "done": false}',
        '{"done": true, "summary": "all set"}',
    ])
    run_team_fn = AsyncMock(return_value="research result")

    result = _execute(
        Delegation(run_team_fn=run_team_fn), team, "Assess launch readiness",
        user_context={"company_id": "acme"},
    )
    assert result == "all set"
    run_team_fn.assert_called_once()

    first_call = team.pis["lead"].run.call_args_list[0]
    assert first_call.kwargs["user_context"]["memory_task_hint"] == "Assess launch readiness"
    assert run_team_fn.call_args.kwargs["user_context"]["memory_task_hint"] == "investigate launch plan"


def test_max_steps_returns_last_result(team):
    team.pis["lead"].run = AsyncMock(
        return_value='{"team": "eng", "mode": "default", "sub_task": "keep going", "done": false}'
    )
    run_team_fn = AsyncMock(return_value="partial result")

    result = _execute(Delegation(run_team_fn=run_team_fn, max_steps=2), team, "complex task")
    assert result == "partial result"
    assert run_team_fn.call_count == 2


def test_no_lead_pi_raises(team):
    team.lead_pi_id = None

    with pytest.raises(TeamError, match="no valid lead_pi"):
        _execute(Delegation(run_team_fn=AsyncMock()), team, "task")


def test_retry_on_bad_json(team):
    team.pis["lead"].run = AsyncMock(side_effect=[
        "I think we should route to research team",
        '{"done": true, "summary": "fixed"}',
    ])

    assert _execute(Delegation(run_team_fn=AsyncMock()), team, "task") == "fixed"
    assert team.pis["lead"].run.call_count == 2


def test_branch_team_failure_with_accumulated(team):
    """If branch team fails but we have previous results, return last."""
    team.pis["lead"].run = AsyncMock(side_effect=[
        '{"team": "a", "sub_task": "step1", "done": false}',
        '{"team": "b", "sub_task": "step2", "done": false}',
    ])
    run_team_fn = AsyncMock(side_effect=["step1 result", Exception("team b crashed")])

    result = _execute(Delegation(run_team_fn=run_team_fn, max_steps=5), team, "multi-step")
    assert result == "step1 result"
