"""Tests for ConditionalMode -lead Pi evaluates and branches."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from companest.modes.conditional import ConditionalMode, _parse_decision
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
    config = TeamConfig(id="test", role="general", lead_pi=pi_ids[0], pis=[PiConfig(id=pid) for pid in pi_ids])
    return AgentTeam(config, mm)


class TestParseDecision:
    def test_pure_json(self):
        raw = '{"team": "research", "mode": "default", "sub_task": "find info", "done": false}'
        result = _parse_decision(raw)
        assert result["team"] == "research"
        assert result["done"] is False

    def test_markdown_code_block(self):
        raw = '```json\n{"team": "eng", "done": true, "summary": "all done"}\n```'
        result = _parse_decision(raw)
        assert result["done"] is True
        assert result["summary"] == "all done"

    def test_surrounding_text(self):
        raw = 'I think the best approach is:\n{"team": "research", "done": false, "sub_task": "look up X"}\nLet me know!'
        result = _parse_decision(raw)
        assert result["team"] == "research"

    def test_nested_braces_in_value(self):
        raw = '{"team": "eng", "sub_task": "parse {data} from input", "done": false}'
        result = _parse_decision(raw)
        assert result is not None
        assert result["team"] == "eng"

    def test_returns_none_for_garbage(self):
        assert _parse_decision("I don't know what to do") is None

    def test_returns_none_for_array(self):
        assert _parse_decision('["not", "a", "decision"]') is None


class TestConditionalExecution:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_single_step_done(self):
        """Lead Pi returns done=true immediately."""
        team = _make_team(self.tmpdir, ["lead"])
        team.pis["lead"].run = AsyncMock(
            return_value='{"done": true, "summary": "Task is trivial, answer is 42"}'
        )
        run_team_fn = AsyncMock()

        mode = ConditionalMode(run_team_fn=run_team_fn)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "What is 6*7-")
        )
        assert "42" in result
        run_team_fn.assert_not_called()

    def test_single_step_route(self):
        """Lead Pi routes to a team, then done."""
        team = _make_team(self.tmpdir, ["lead"])

        # First call: route to research; Second call: done
        team.pis["lead"].run = AsyncMock(side_effect=[
            '{"team": "research", "mode": "default", "sub_task": "look up AI trends", "done": false}',
            '{"done": true, "summary": "AI is growing fast"}',
        ])
        run_team_fn = AsyncMock(return_value="Research found: AI is growing fast")

        mode = ConditionalMode(run_team_fn=run_team_fn)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "Tell me about AI trends")
        )
        assert "AI" in result
        run_team_fn.assert_called_once()

    def test_max_steps_returns_last_result(self):
        """Hitting max_steps returns the last accumulated result."""
        team = _make_team(self.tmpdir, ["lead"])
        team.pis["lead"].run = AsyncMock(
            return_value='{"team": "eng", "mode": "default", "sub_task": "keep going", "done": false}'
        )
        run_team_fn = AsyncMock(return_value="partial result")

        mode = ConditionalMode(run_team_fn=run_team_fn, max_steps=2)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "complex task")
        )
        assert result == "partial result"
        assert run_team_fn.call_count == 2

    def test_no_lead_pi_raises(self):
        team = _make_team(self.tmpdir, ["lead"])
        team.lead_pi_id = None
        mode = ConditionalMode(run_team_fn=AsyncMock())

        with pytest.raises(TeamError, match="no valid lead_pi"):
            asyncio.get_event_loop().run_until_complete(
                mode.execute(team, "task")
            )

    def test_retry_on_bad_json(self):
        """First response is not JSON, retry should be attempted."""
        team = _make_team(self.tmpdir, ["lead"])
        team.pis["lead"].run = AsyncMock(side_effect=[
            "I think we should route to research team",  # bad JSON
            '{"done": true, "summary": "fixed"}',         # retry succeeds
        ])
        run_team_fn = AsyncMock()

        mode = ConditionalMode(run_team_fn=run_team_fn)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "task")
        )
        assert result == "fixed"
        # 2 calls: original + retry
        assert team.pis["lead"].run.call_count == 2

    def test_branch_team_failure_with_accumulated(self):
        """If branch team fails but we have previous results, return last."""
        team = _make_team(self.tmpdir, ["lead"])
        team.pis["lead"].run = AsyncMock(side_effect=[
            '{"team": "a", "sub_task": "step1", "done": false}',
            '{"team": "b", "sub_task": "step2", "done": false}',
        ])
        run_team_fn = AsyncMock(side_effect=[
            "step1 result",
            Exception("team b crashed"),
        ])

        mode = ConditionalMode(run_team_fn=run_team_fn, max_steps=5)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "multi-step")
        )
        assert result == "step1 result"



def test_conditional_passes_memory_task_hint(tmp_path):
    team = _make_team(str(tmp_path), ["lead"])
    team.pis["lead"].run = AsyncMock(side_effect=[
        '{"team": "research", "mode": "default", "sub_task": "investigate launch plan", "done": false}',
        '{"done": true, "summary": "all set"}',
    ])
    run_team_fn = AsyncMock(return_value="research result")

    mode = ConditionalMode(run_team_fn=run_team_fn)
    asyncio.get_event_loop().run_until_complete(
        mode.execute(team, "Assess launch readiness", user_context={"company_id": "acme"})
    )

    first_call = team.pis["lead"].run.call_args_list[0]
    assert first_call.kwargs["user_context"]["memory_task_hint"] == "Assess launch readiness"

    branch_call = run_team_fn.call_args
    assert branch_call.kwargs["user_context"]["memory_task_hint"] == "investigate launch plan"
