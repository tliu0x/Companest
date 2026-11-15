import pytest


class TestTools:
    """Tool name resolution, presets, registry providers and deny lists."""


    def test_deduplication(self):
        from companest.agents.tools import resolve_tool_names
        assert resolve_tool_names(["Read", "Read", "read"]).count("Read") == 1

    @pytest.mark.parametrize("deny,allowed", [
        ({"Bash"}, ["Read", "Write"]),
        ({"Bash", "Write"}, ["Read"]),
    ])
    def test_resolve_with_deny_filter(self, deny, allowed):
        from companest.agents.tools import ToolRegistry
        result = ToolRegistry().resolve_tool_names(["Read", "Write", "Bash"], tools_deny=deny)
        for name in ("Read", "Write", "Bash"):
            assert (name in result) == (name in allowed)


    @pytest.mark.parametrize("deny_line,expected", [
        ("- tools_deny: Bash, Write\n", ["Bash", "Write"]),
        ("", []),
    ])
    def test_parse_tools_deny_from_markdown(self, deny_line, expected):
        from companest.agents.team import _parse_pi_sections
        text = (
            "# Team: test\n"
            "#### Pi: agent1\n"
            "- model: claude-haiku-4-5-20251001\n"
            "- tools: Read, Write\n"
            f"{deny_line}"
            "- max_turns: 5\n"
        )
        pis = _parse_pi_sections(text)
        assert len(pis) == 1
        assert pis[0].tools_deny == expected
