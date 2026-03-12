"""
Execution Modes Unit Tests

Tests for the companest/modes/ package:
1. ModeRegistry  register, get, list
2. ExecutionMode interface  all built-in modes implement it
3. DefaultMode  delegates to lead Pi
4. Backward compat  AgentTeam.run/run_loop/run_council/run_collaborative still work
"""

import asyncio
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from companest.modes import (
    ExecutionMode,
    ModeRegistry,
    build_default_registry,
    VALID_MODES,
    DefaultMode,
    LoopMode,
    CouncilMode,
    CollaborativeMode,
)
from companest.team import AgentTeam, TeamConfig
from companest.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import TeamError


def _make_team(tmpdir: str, pi_ids: list[str]) -> AgentTeam:
    """Build an AgentTeam with the given Pi IDs (mocked  no soul.md needed)."""
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


#  ModeRegistry Tests 

class TestModeRegistry:
    """Tests for ModeRegistry class."""

    def test_build_default_registry(self):
        registry = build_default_registry()
        modes = registry.list_modes()
        assert "default" in modes
        assert "cascade" in modes
        assert "loop" in modes
        assert "council" in modes
        assert "collaborative" in modes

    def test_get_registered_mode(self):
        registry = build_default_registry()
        mode = registry.get("default")
        assert isinstance(mode, DefaultMode)

    def test_get_cascade_is_default_with_cascade(self):
        registry = build_default_registry()
        mode = registry.get("cascade")
        assert isinstance(mode, DefaultMode)
        assert mode._cascade is True

    def test_get_unknown_raises(self):
        registry = build_default_registry()
        with pytest.raises(KeyError, match="Unknown execution mode"):
            registry.get("nonexistent")

    def test_register_custom_mode(self):
        registry = ModeRegistry()

        class CustomMode(ExecutionMode):
            @property
            def name(self):
                return "custom"

            async def execute(self, team, task, on_progress=None, user_context=None):
                return "custom result"

        custom = CustomMode()
        registry.register(custom)
        assert "custom" in registry.list_modes()
        assert registry.get("custom") is custom

    def test_register_with_name_override(self):
        registry = ModeRegistry()
        mode = DefaultMode(cascade=True)
        registry.register(mode, name="my_cascade")
        assert "my_cascade" in registry.list_modes()
        assert registry.get("my_cascade") is mode

    def test_valid_modes_tuple(self):
        assert isinstance(VALID_MODES, tuple)
        assert "default" in VALID_MODES
        assert "cascade" in VALID_MODES
        assert "loop" in VALID_MODES
        assert "council" in VALID_MODES


#  Mode Interface Tests 

class TestModeInterface:
    """All built-in modes implement ExecutionMode correctly."""

    def test_default_mode_name(self):
        mode = DefaultMode()
        assert mode.name == "default"
        assert isinstance(mode, ExecutionMode)

    def test_cascade_mode_is_default(self):
        mode = DefaultMode(cascade=True)
        assert mode.name == "cascade"
        assert mode._cascade is True

    def test_loop_mode_name(self):
        mode = LoopMode()
        assert mode.name == "loop"
        assert isinstance(mode, ExecutionMode)

    def test_council_mode_name(self):
        mode = CouncilMode()
        assert mode.name == "council"
        assert isinstance(mode, ExecutionMode)

    def test_collaborative_mode_name(self):
        mode = CollaborativeMode()
        assert mode.name == "collaborative"
        assert isinstance(mode, ExecutionMode)

#  DefaultMode Execution Tests 

class TestDefaultModeExecution:
    """Test DefaultMode.execute() delegates correctly."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_default_mode_calls_lead_pi(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="answer")

        mode = DefaultMode(cascade=False)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "What is 2+2-")
        )
        assert result == "answer"
        team.pis["alpha"].run.assert_called_once()
        # Verify cascade=False was passed
        call_kwargs = team.pis["alpha"].run.call_args
        assert call_kwargs.kwargs.get("cascade") is False or call_kwargs[1].get("cascade") is False

    def test_cascade_mode_calls_with_cascade_true(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="cascade answer")

        mode = DefaultMode(cascade=True)
        result = asyncio.get_event_loop().run_until_complete(
            mode.execute(team, "task")
        )
        assert result == "cascade answer"
        call_kwargs = team.pis["alpha"].run.call_args
        assert call_kwargs.kwargs.get("cascade") is True or call_kwargs[1].get("cascade") is True

    def test_no_lead_pi_raises(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.lead_pi_id = None

        mode = DefaultMode()
        with pytest.raises(TeamError, match="no valid lead_pi"):
            asyncio.get_event_loop().run_until_complete(
                mode.execute(team, "task")
            )


#  Backward Compatibility Tests 

class TestBackwardCompat:
    """AgentTeam methods still work as thin delegators."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_team_run_delegates(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="result")

        result = asyncio.get_event_loop().run_until_complete(
            team.run("task")
        )
        assert result == "result"

    def test_team_run_with_cascade(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="cascade result")

        result = asyncio.get_event_loop().run_until_complete(
            team.run("task", cascade=True)
        )
        assert result == "cascade result"

    def test_team_run_council_delegates(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            "Synthesized",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("task")
        )
        assert result == "Synthesized"

    def test_team_run_collaborative_delegates(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        team.pis["alpha"].run = AsyncMock(return_value="step1 output")
        team.pis["beta"].run = AsyncMock(return_value="final output")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_collaborative("input task")
        )
        assert result == "final output"
        # Alpha gets the original task, Beta gets Alpha's output
        team.pis["alpha"].run.assert_called_once()
        team.pis["beta"].run.assert_called_once()


#  Mode Imports 

class TestModeImports:
    """Verify imports work from the Companest public API."""

    def test_import_from_companest_package(self):
        from companest import ExecutionMode, ModeRegistry, DefaultMode, LoopMode, CouncilMode

    def test_import_from_companest_modes(self):
        from companest.modes import ExecutionMode, ModeRegistry, VALID_MODES

    def test_import_parse_judge_from_council(self):
        from companest.modes.council import _parse_judge_response

    def test_valid_modes_from_router(self):
        from companest.router import VALID_MODES as router_modes
        from companest.modes import VALID_MODES as modes_valid
        assert router_modes is modes_valid
