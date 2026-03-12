"""Tests for CollaborativeMode -multi-Pi pipeline execution."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from companest.modes.collaborative import CollaborativeMode
from companest.team import AgentTeam, TeamConfig
from companest.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import TeamError


def _make_team(tmpdir: str, pi_ids: list) -> AgentTeam:
    base = Path(tmpdir)
    (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
    for pid in pi_ids:
        (base / "teams" / "test" / "pis" / pid).mkdir(parents=True, exist_ok=True)
        (base / "teams" / "test" / "pis" / pid / "soul.md").write_text(f"Pi {pid}")
    (base / "soul.md").write_text("soul")
    (base / "user.md").write_text("user")

    mm = MemoryManager(str(base))
    config = TeamConfig(
        id="test",
        role="general",
        lead_pi=pi_ids[0],
        pis=[PiConfig(id=pid) for pid in pi_ids],
    )
    return AgentTeam(config, mm)


class TestCollaborativePipeline:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_two_stage_pipeline(self):
        team = _make_team(self.tmpdir, ["researcher", "writer"])
        team.pis["researcher"].run = AsyncMock(return_value="research findings")
        team.pis["writer"].run = AsyncMock(return_value="polished article")

        mode = CollaborativeMode()
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "Write about AI")
        )
        assert result == "polished article"
        team.pis["researcher"].run.assert_called_once()
        team.pis["writer"].run.assert_called_once()

    def test_three_stage_pipeline(self):
        team = _make_team(self.tmpdir, ["a", "b", "c"])
        team.pis["a"].run = AsyncMock(return_value="step1")
        team.pis["b"].run = AsyncMock(return_value="step2")
        team.pis["c"].run = AsyncMock(return_value="step3")

        mode = CollaborativeMode()
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "task")
        )
        assert result == "step3"
        assert team.pis["a"].run.call_count == 1
        assert team.pis["b"].run.call_count == 1
        assert team.pis["c"].run.call_count == 1

    def test_custom_pipeline_order(self):
        team = _make_team(self.tmpdir, ["a", "b", "c"])
        team.pis["a"].run = AsyncMock(return_value="a_out")
        team.pis["c"].run = AsyncMock(return_value="c_out")

        mode = CollaborativeMode(pipeline=["c", "a"])
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "task")
        )
        assert result == "a_out"
        # b should not be called
        assert not hasattr(team.pis["b"].run, "call_count") or team.pis["b"].run.call_count == 0

    def test_missing_pi_raises(self):
        team = _make_team(self.tmpdir, ["a"])
        mode = CollaborativeMode(pipeline=["a", "nonexistent"])

        with pytest.raises(TeamError, match="not found"):
            asyncio.get_event_loop().run_until_complete(
                mode.execute(team, "task")
            )

    def test_stop_on_failure_true(self):
        team = _make_team(self.tmpdir, ["a", "b"])
        team.pis["a"].run = AsyncMock(side_effect=Exception("boom"))
        team.pis["b"].run = AsyncMock(return_value="ok")

        mode = CollaborativeMode(stop_on_failure=True)
        with pytest.raises(TeamError, match="Pipeline failed"):
            asyncio.get_event_loop().run_until_complete(
                mode.execute(team, "task")
            )
        # b should not be called
        team.pis["b"].run.assert_not_called()

    def test_stop_on_failure_false_skips(self):
        team = _make_team(self.tmpdir, ["a", "b"])
        team.pis["a"].run = AsyncMock(side_effect=Exception("boom"))
        team.pis["b"].run = AsyncMock(return_value="recovered")

        mode = CollaborativeMode(stop_on_failure=False)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "original task")
        )
        assert result == "recovered"
        team.pis["b"].run.assert_called_once()

    def test_progress_callback(self):
        team = _make_team(self.tmpdir, ["a", "b"])
        team.pis["a"].run = AsyncMock(return_value="out1")
        team.pis["b"].run = AsyncMock(return_value="out2")
        progress_messages = []

        async def on_progress(msg):
            progress_messages.append(msg)

        mode = CollaborativeMode()
        asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "task", on_progress=on_progress)
        )
        assert len(progress_messages) == 2
        assert "stage 1/2" in progress_messages[0]
        assert "stage 2/2" in progress_messages[1]

    def test_stage_prompt_contains_original_task(self):
        team = _make_team(self.tmpdir, ["a", "b"])
        team.pis["a"].run = AsyncMock(return_value="a_output")
        team.pis["b"].run = AsyncMock(return_value="b_output")

        mode = CollaborativeMode()
        asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "Analyze market trends")
        )
        # Both stages should receive the original task in their prompt
        a_prompt = team.pis["a"].run.call_args[0][0]
        b_prompt = team.pis["b"].run.call_args[0][0]
        assert "Analyze market trends" in a_prompt
        assert "Analyze market trends" in b_prompt
        # Second stage should receive first stage's output
        assert "a_output" in b_prompt



def test_collaborative_passes_memory_task_hint(tmp_path):
    team = _make_team(str(tmp_path), ["a", "b"])
    team.pis["a"].run = AsyncMock(return_value="a_output")
    team.pis["b"].run = AsyncMock(return_value="b_output")

    mode = CollaborativeMode()
    asyncio.get_event_loop().run_until_complete(
        mode.execute(team, "Analyze market trends", user_context={"company_id": "acme"})
    )

    a_call = team.pis["a"].run.call_args
    b_call = team.pis["b"].run.call_args
    assert a_call.kwargs["user_context"]["memory_task_hint"] == "Analyze market trends"
    assert b_call.kwargs["user_context"]["memory_task_hint"] == "Analyze market trends"
