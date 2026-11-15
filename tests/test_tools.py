import pytest

from companest.memory import MemoryManager


class TestTools:
    """Tool name resolution, presets, registry providers and deny lists."""

    @pytest.mark.parametrize("config,expected", [
        (["web_search", "exec"], {"WebSearch", "Bash"}),
        (["memory_read", "memory_write"], {"memory_read", "memory_write"}),
        (["researcher"], {"WebSearch", "memory_read"}),
        (["messenger"], {"sessions_send"}),
        (["sessions_send", "sessions_list", "sessions_history"], {
            "sessions_send",
            "sessions_list",
            "sessions_history",
        }),
    ])
    def test_resolve_contains(self, config, expected):
        from companest.agents.tools import resolve_tool_names
        assert expected <= set(resolve_tool_names(config))

    def test_resolve_builtin_keeps_order(self):
        from companest.agents.tools import resolve_tool_names
        assert resolve_tool_names(["Read", "Write", "Bash"]) == ["Read", "Write", "Bash"]

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

    def test_default_deny_list(self):
        """Default PiConfig.tools_deny=[] falls back to DEFAULT_TOOLS_DENY, which bans Bash."""
        from companest.agents.pi import PiConfig
        from companest.agents.tools import DEFAULT_TOOLS_DENY
        assert PiConfig(id="test").tools_deny == []
        assert "Bash" in DEFAULT_TOOLS_DENY

    def test_deny_none_allows_all(self, tmp_path):
        from companest.agents.pi import Pi, PiConfig

        (tmp_path / "teams" / "test" / "memory").mkdir(parents=True)
        config = PiConfig(id="pi1", tools=["Read", "Bash"], tools_deny=["none"])
        pi = Pi(config, MemoryManager(str(tmp_path)), team_id="test")
        assert pi._build_deny_set() == set()

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
