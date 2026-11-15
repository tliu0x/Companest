"""Verify all default model configurations converge on deepseek-chat."""

from companest.company import CompanyCEOConfig
from companest.pi import PiConfig
from companest.team import _parse_pi_sections


def test_default_models_converge():
    team_md = (
        "# Team: test-team\n"
        "- role: general\n"
        "- lead_pi: agent\n"
        "\n"
        "#### Pi: agent\n"
        "- tools: memory_read\n"
        "- max_turns: 5\n"
    )
    pis = _parse_pi_sections(team_md)
    assert len(pis) == 1
    assert pis[0].model == PiConfig(id="test").model == CompanyCEOConfig().model == "deepseek-chat"
