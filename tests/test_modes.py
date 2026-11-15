"""Execution Modes Unit Tests"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from companest.modes import ModeRegistry, build_default_registry, DefaultMode
from companest.team import AgentTeam, TeamConfig
from companest.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import TeamError


def _make_team(base, pi_ids) -> AgentTeam:
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
    return AgentTeam(config, MemoryManager(str(base)))


class TestModeRegistry:
    def test_build_default_registry(self):
        registry = build_default_registry()
        modes = registry.list_modes()
        for name in ("default", "cascade", "loop", "council", "collaborative"):
            assert name in modes
        assert isinstance(registry.get("default"), DefaultMode)
        assert registry.get("default")._cascade is False
        assert isinstance(registry.get("cascade"), DefaultMode)
        assert registry.get("cascade")._cascade is True

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown execution mode"):
            build_default_registry().get("nonexistent")

    def test_register_with_name_override(self):
        registry = ModeRegistry()
        mode = DefaultMode(cascade=True)
        assert mode.name == "cascade"
        assert DefaultMode().name == "default"
        registry.register(mode, name="my_cascade")
        assert registry.list_modes() == ["my_cascade"]
        assert registry.get("my_cascade") is mode


class TestDefaultModeExecution:
    @pytest.mark.parametrize("cascade", [False, True])
    def test_calls_lead_pi_with_cascade_flag(self, tmp_path, cascade):
        team = _make_team(tmp_path, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="answer")

        result = asyncio.get_event_loop().run_until_complete(
            DefaultMode(cascade=cascade).execute(team, "What is 2+2-")
        )
        assert result == "answer"
        team.pis["alpha"].run.assert_called_once()
        assert team.pis["alpha"].run.call_args.kwargs["cascade"] is cascade

    def test_no_lead_pi_raises(self, tmp_path):
        team = _make_team(tmp_path, ["alpha"])
        team.lead_pi_id = None

        with pytest.raises(TeamError, match="no valid lead_pi"):
            asyncio.get_event_loop().run_until_complete(DefaultMode().execute(team, "task"))
