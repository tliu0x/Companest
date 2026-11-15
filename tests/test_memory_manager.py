import pytest

from companest.memory import MemoryManager

from design_helpers import _make_team


class TestMemoryPrompts:
    """MemoryManager soul/user reads and system prompt enrichment."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        _make_team(tmp_path, "stock", "analyst", team_soul="Stock team soul",
                   pi_soul="You are a stock analyst.")
        _make_team(tmp_path, "research", "scout", role="research",
                   extra="- always_on: true\n", team_soul="Research team soul",
                   pi_soul="You are a research scout.")
        _make_team(tmp_path, "data", "collector", role="data", pi_soul="Data collector.")
        (tmp_path / "soul.md").write_text("Master soul")
        (tmp_path / "user.md").write_text("# User\n- Language: English")
        self.mm = MemoryManager(str(tmp_path))

    def _write_briefing(self, items):
        self.mm.write_team_memory("research", "briefing.json", {
            "updated_at": "2025-01-15T10:30:00Z",
            "items": items,
        })

    def test_read_master_and_pi_files(self):
        assert "English" in self.mm.read_master_user()
        assert "analyst" in self.mm.read_pi_soul("stock", "analyst").lower()

    def test_list_teams_and_team_exists(self):
        assert "stock" in self.mm.list_teams()

    def test_build_system_prompt(self):
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "stock analyst" in prompt.lower()
        assert "English" in prompt
        assert "World Briefing" not in prompt

    def test_briefing_injected_into_business_team(self):
        self._write_briefing([
            {"headline": "S&P 500 hits record high", "category": "markets", "source": "Reuters"},
            {"headline": "New AI chip announced", "category": "tech", "source": "TechCrunch"},
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "S&P 500 hits record high" in prompt
        assert "New AI chip announced" in prompt
        assert "[markets]" in prompt
        assert "(Reuters)" in prompt

    def test_not_injected_into_research_team(self):
        """Research team does NOT get its own briefing (anti-circular)."""
        self._write_briefing([
            {"headline": "Test headline", "category": "world", "source": "AP"},
        ])
        prompt = self.mm.build_system_prompt("research", "scout")
        assert "World Briefing" not in prompt

    def test_capped_at_max_items(self):
        self._write_briefing([
            {"headline": f"Item {i}", "category": "world", "source": "AP"}
            for i in range(20)
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "Item 14" in prompt
        assert "Item 15" not in prompt

    def test_empty_items_no_section(self):
        self._write_briefing([])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_malformed_briefing_no_crash(self):
        self.mm.write_team_memory("research", "briefing.json", "not a dict")
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" not in prompt

    def test_items_missing_headline_skipped(self):
        self._write_briefing([
            {"category": "world", "source": "AP"},
            {"headline": "Valid headline", "category": "tech", "source": "BBC"},
        ])
        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "World Briefing" in prompt
        assert "Valid headline" in prompt
        briefing_section = prompt.split("World Briefing")[1]
        assert briefing_section.count("- ") == 1

    def test_custom_enrichment_injected(self):
        from companest.memory import EnrichmentSource

        def format_prices(data):
            if not isinstance(data, dict):
                return None
            prices = data.get("prices", [])
            if not prices:
                return None
            lines = [f"- {p['symbol']}: ${p['price']}" for p in prices]
            return "## Market Prices\n" + "\n".join(lines)

        self.mm.register_enrichment(EnrichmentSource(
            source_team_id="data",
            memory_key="prices.json",
            section_title="Market Prices",
            formatter=format_prices,
            exclude_teams={"data"},
        ))
        self.mm.write_team_memory("data", "prices.json", {
            "prices": [{"symbol": "TSLA", "price": 250.0}]
        })

        prompt = self.mm.build_system_prompt("stock", "analyst")
        assert "Market Prices" in prompt
        assert "TSLA" in prompt

    def test_custom_enrichment_excluded_for_source_team(self):
        from companest.memory import EnrichmentSource

        self.mm.register_enrichment(EnrichmentSource(
            source_team_id="data",
            memory_key="stuff.json",
            section_title="Custom",
            formatter=lambda data: "## Custom Section\nSome data",
            exclude_teams={"data"},
        ))
        self.mm.write_team_memory("data", "stuff.json", {"anything": True})

        prompt = self.mm.build_system_prompt("data", "collector")
        assert "Custom Section" not in prompt
