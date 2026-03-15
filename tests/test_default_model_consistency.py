"""Verify all default model configurations converge on deepseek-chat.

After the cost-reduction PR, every component that picks a default model
should agree on 'deepseek-chat' (not a mix of Claude and DeepSeek).
"""

from companest.company import CompanyCEOConfig
from companest.pi import PiConfig
from companest.team import _parse_pi_sections


EXPECTED_DEFAULT = "deepseek-chat"


def test_pi_config_default_model():
    """PiConfig default should be deepseek-chat."""
    cfg = PiConfig(id="test")
    assert cfg.model == EXPECTED_DEFAULT


def test_company_ceo_default_model():
    """CompanyCEOConfig default should be deepseek-chat."""
    cfg = CompanyCEOConfig()
    assert cfg.model == EXPECTED_DEFAULT


def test_team_md_parser_default_model():
    """When team.md omits the model field, parser should default to deepseek-chat."""
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
    assert pis[0].model == EXPECTED_DEFAULT
