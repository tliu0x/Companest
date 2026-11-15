import pytest

from companest.memory import MemoryManager

from design_helpers import _make_team


class TestTeamRegistry:
    """Team declarations and per-invocation harness construction."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from companest.agents.team import TeamRegistry

        self.base = tmp_path
        _make_team(
            tmp_path, "stock", "analyst", extra="- always_on: false\n",
            pi_md="\n#### Pi: analyst\n- model: claude-sonnet-4-5-20250929\n"
                  "- tools: memory_read, web_search\n- max_turns: 5\n",
        )
        _make_team(
            tmp_path, "finance", "accountant", role="cost_gate", extra="- always_on: true\n",
            pi_md="\n#### Pi: accountant\n- model: claude-haiku-4-5-20251001\n"
                  "- tools: memory_read, memory_write\n- max_turns: 3\n",
        )
        self.mm = MemoryManager(str(tmp_path))
        self.reg = TeamRegistry(str(tmp_path / "teams"), self.mm)
        self.reg.scan_configs()

    def test_team_config_from_markdown(self):
        from companest.agents.team import TeamConfig
        config = TeamConfig.from_markdown(self.base / "teams" / "stock" / "team.md")
        assert config.id == "stock"
        assert config.role == "general"
        assert config.lead_pi == "analyst"
        assert not config.always_on
        assert len(config.pis) == 1
        assert config.pis[0].id == "analyst"
        assert config.pis[0].model == "claude-sonnet-4-5-20250929"

    def test_team_config_discovers_pis_from_directories(self):
        """team.md without Pi sections falls back to the pis/ subdirectories."""
        from companest.agents.team import TeamConfig
        _make_team(self.base, "research", "scout", role="research", extra="- always_on: true\n")
        config = TeamConfig.from_markdown(self.base / "teams" / "research" / "team.md")
        assert config.id == "research"
        assert config.role == "research"
        assert config.always_on is True
        assert config.lead_pi == "scout"
        assert [p.id for p in config.pis] == ["scout"]


    def test_registry_on_demand_create(self):
        assert "stock" not in self.reg.list_active()
        team = self.reg.create("stock")
        assert team.id == "stock"
        assert team is not self.reg.create("stock")

    def test_registry_get_nonexistent_raises(self):
        from companest.exceptions import TeamError
        with pytest.raises(TeamError):
            self.reg.create("nonexistent")


    def test_register_on_demand_team(self):
        from companest.agents.team import TeamConfig
        from companest.agents.pi import PiConfig

        self.reg.register(TeamConfig(
            id="kalshi",
            role="trading",
            lead_pi="trader",
            pis=[PiConfig(id="trader", model="claude-haiku-4-5-20251001", tools=["memory_read"])],
        ))

        assert "kalshi" in self.reg.list_teams()
        assert self.reg.create("kalshi").id == "kalshi"

    def test_register_always_on_team(self):
        from companest.agents.team import TeamConfig
        from companest.agents.pi import PiConfig

        self.reg.register(TeamConfig(
            id="monitor",
            role="monitoring",
            lead_pi="watcher",
            pis=[PiConfig(id="watcher")],
        ), always_on=True)

        assert "monitor" in self.reg.list_meta_teams()
        assert self.reg.list_active() == []

    def test_register_disabled_team_skipped(self):
        from companest.agents.team import TeamConfig
        self.reg.register(TeamConfig(id="disabled", enabled=False))
        assert "disabled" not in self.reg.list_teams()
