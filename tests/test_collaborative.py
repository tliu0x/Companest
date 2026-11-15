"""Tests for CollaborativeMode -multi-Pi pipeline execution."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from companest.modes.collaborative import CollaborativeMode
from companest.agents.team import AgentTeam, TeamConfig
from companest.agents.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import TeamError


def _make_team(base, pi_ids, outputs=None) -> AgentTeam:
    """Build a team; `outputs` maps pi_id -> mocked run() result (or exception)."""
    (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
    for pid in pi_ids:
        (base / "teams" / "test" / "pis" / pid).mkdir(parents=True, exist_ok=True)
        (base / "teams" / "test" / "pis" / pid / "soul.md").write_text(f"Pi {pid}")
    (base / "soul.md").write_text("soul")
    (base / "user.md").write_text("user")
    config = TeamConfig(
        id="test", role="general", lead_pi=pi_ids[0],
        pis=[PiConfig(id=pid) for pid in pi_ids],
    )
    team = AgentTeam(config, MemoryManager(str(base)))
    for pid, out in (outputs or {}).items():
        if isinstance(out, Exception):
            team.pis[pid].run = AsyncMock(side_effect=out)
        else:
            team.pis[pid].run = AsyncMock(return_value=out)
    return team


def _execute(mode, team, task, **kwargs):
    return asyncio.get_event_loop().run_until_complete(mode.execute(team, task, **kwargs))


def test_pipeline_chains_stages_and_passes_task_and_memory_hint(tmp_path):
    team = _make_team(tmp_path, ["a", "b"], {"a": "a_output", "b": "b_output"})

    result = _execute(
        CollaborativeMode(), team, "Analyze market trends", user_context={"company_id": "acme"},
    )
    assert result == "b_output"
    team.pis["a"].run.assert_called_once()
    team.pis["b"].run.assert_called_once()

    a_call = team.pis["a"].run.call_args
    b_call = team.pis["b"].run.call_args
    assert "Analyze market trends" in a_call[0][0]
    assert "Analyze market trends" in b_call[0][0]
    assert "a_output" in b_call[0][0]
    assert a_call.kwargs["user_context"]["memory_task_hint"] == "Analyze market trends"
    assert b_call.kwargs["user_context"]["memory_task_hint"] == "Analyze market trends"


def test_custom_pipeline_order(tmp_path):
    team = _make_team(tmp_path, ["a", "b", "c"], {"a": "a_out", "b": "b_out", "c": "c_out"})

    assert _execute(CollaborativeMode(pipeline=["c", "a"]), team, "task") == "a_out"
    assert "c_out" in team.pis["a"].run.call_args[0][0]
    team.pis["b"].run.assert_not_called()


def test_missing_pi_raises(tmp_path):
    team = _make_team(tmp_path, ["a"])

    with pytest.raises(TeamError, match="not found"):
        _execute(CollaborativeMode(pipeline=["a", "nonexistent"]), team, "task")


def test_stop_on_failure_true(tmp_path):
    team = _make_team(tmp_path, ["a", "b"], {"a": Exception("boom"), "b": "ok"})

    with pytest.raises(TeamError, match="Pipeline failed"):
        _execute(CollaborativeMode(stop_on_failure=True), team, "task")
    team.pis["b"].run.assert_not_called()


def test_stop_on_failure_false_skips(tmp_path):
    team = _make_team(tmp_path, ["a", "b"], {"a": Exception("boom"), "b": "recovered"})

    assert _execute(CollaborativeMode(stop_on_failure=False), team, "original task") == "recovered"
    team.pis["b"].run.assert_called_once()


def test_progress_callback(tmp_path):
    team = _make_team(tmp_path, ["a", "b"], {"a": "out1", "b": "out2"})
    progress_messages = []

    async def on_progress(msg):
        progress_messages.append(msg)

    _execute(CollaborativeMode(), team, "task", on_progress=on_progress)
    assert len(progress_messages) == 2
    assert "stage 1/2" in progress_messages[0]
    assert "stage 2/2" in progress_messages[1]
