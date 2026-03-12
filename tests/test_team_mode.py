from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from companest.config import CompanestConfig
from companest.orchestrator import CompanestOrchestrator
from companest.team import TeamConfig


def test_team_config_parses_mode(tmp_path):
    team_dir = tmp_path / "teams" / "research"
    team_dir.mkdir(parents=True)
    (team_dir / "team.md").write_text(
        "# Team: research\n"
        "- role: Research\n"
        "- mode: council\n"
        "- lead_pi: analyst\n",
        encoding="utf-8",
    )

    config = TeamConfig.from_markdown(team_dir / "team.md")

    assert config.mode == "council"


@pytest.mark.asyncio
async def test_run_team_uses_team_default_mode_when_not_overridden():
    orchestrator = CompanestOrchestrator(CompanestConfig(debug=True))
    execution_mode = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    team = SimpleNamespace(
        mode="council",
        pis={},
        get_lead_config=lambda: None,
    )

    orchestrator.team_registry = MagicMock()
    orchestrator.team_registry.get_or_create.return_value = team
    orchestrator.mode_registry = MagicMock()
    orchestrator.mode_registry.get.return_value = execution_mode
    orchestrator.mode_registry.list_modes.return_value = ["default", "council"]
    orchestrator.events = MagicMock()
    orchestrator.events.emit = AsyncMock()

    result = await orchestrator.run_team(
        task="Analyze this topic",
        team_id="research",
        skip_cost_check=True,
        mode=None,
    )

    assert result == "ok"
    orchestrator.mode_registry.get.assert_called_once_with("council")
    execution_mode.execute.assert_awaited_once_with(
        team,
        "Analyze this topic",
        on_progress=None,
        user_context={},
    )