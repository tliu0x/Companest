"""
Integration tests for the info-collection system.

Tests the full stack:
1. Feed fetchers (live HTTP calls to Reddit, HN)
2. Tool resolution and preset expansion
3. Memory integration (feed.json  system prompt injection)
4. Orchestrator wiring (scheduled + on-demand collection)
"""

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companest.feeds import fetch_hn, fetch_openbb, fetch_reddit, fetch_rss
from companest.memory import MemoryManager
from companest.tools import (
    FEED_TOOL_NAMES,
    TOOL_PRESETS,
    resolve_tool_names,
)


#  1. Feed Fetchers (live HTTP) 

class TestFeedFetchersLive:
    """Test feed fetchers against real APIs (requires network)."""

    @pytest.mark.asyncio
    @pytest.mark.live_network
    async def test_fetch_reddit_returns_items(self):
        items = await fetch_reddit("LocalLLaMA", "hot", 3)
        assert len(items) > 0
        item = items[0]
        assert "title" in item
        assert "url" in item
        assert "source" in item
        assert item["source"] == "r/LocalLLaMA"

    @pytest.mark.asyncio
    @pytest.mark.live_network
    async def test_fetch_hn_returns_items(self):
        items = await fetch_hn("top", 3)
        assert len(items) > 0
        item = items[0]
        assert "title" in item
        assert "url" in item
        assert item["source"] == "hn"

    @pytest.mark.asyncio
    @pytest.mark.live_network
    async def test_fetch_rss_returns_items(self):
        items = await fetch_rss("https://simonwillison.net/atom/everything", 3)
        assert len(items) > 0
        item = items[0]
        assert "title" in item
        assert item["source"] == "rss"

    @pytest.mark.asyncio
    async def test_fetch_reddit_bad_subreddit(self):
        items = await fetch_reddit("this_subreddit_does_not_exist_99999", "hot", 3)
        # Should return error dict or empty, not crash
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_brave_search_no_key(self):
        from companest.feeds import brave_search
        items = await brave_search("test query")
        # No BRAVE_API_KEY set  returns error item
        assert len(items) == 1
        assert "error" in items[0]

    @pytest.mark.asyncio
    async def test_fetch_x_no_key(self):
        from companest.feeds import fetch_x
        items = await fetch_x("karpathy")
        # No X_BEARER_TOKEN set  returns error item
        assert len(items) == 1
        assert "error" in items[0]

    @pytest.mark.asyncio
    async def test_fetch_openbb_no_server(self):
        """fetch_openbb returns error when no OpenBB server is running."""
        items = await fetch_openbb("AAPL", "quote", base_url="http://127.0.0.1:19999")
        assert len(items) == 1
        assert "error" in items[0]
        assert "OpenBB API unavailable" in items[0]["error"]

    @pytest.mark.asyncio
    async def test_fetch_openbb_bad_data_type(self):
        items = await fetch_openbb("AAPL", "nonexistent")
        assert len(items) == 1
        assert "error" in items[0]
        assert "Unknown data_type" in items[0]["error"]

    @pytest.mark.asyncio
    async def test_fetch_openbb_normalize_quotes(self):
        """Test quote normalization with mocked API response."""
        from companest.feeds import _normalize_quotes
        results = [
            {
                "symbol": "AAPL",
                "last_price": 245.30,
                "change_percent": 1.25,
                "volume": 52_300_000,
                "market_cap": 3.8e12,
                "pe_ratio": 31.2,
            },
        ]
        items = _normalize_quotes(results, "2026-02-23T10:00:00Z")
        assert len(items) == 1
        assert items[0]["title"] == "AAPL: $245.3 (+1.25%)"
        assert items[0]["source"] == "openbb/quote"
        assert "Vol:" in items[0]["snippet"]
        assert "$3.8T" in items[0]["snippet"]
        assert "P/E: 31.2" in items[0]["snippet"]

    @pytest.mark.asyncio
    async def test_fetch_openbb_normalize_news(self):
        """Test news normalization with mocked API response."""
        from companest.feeds import _normalize_news
        results = [
            {
                "title": "Fed holds rates steady",
                "url": "https://example.com/fed",
                "source": "Reuters",
                "text": "The Federal Reserve...",
                "date": "2026-02-23T08:00:00Z",
            },
        ]
        items = _normalize_news(results)
        assert len(items) == 1
        assert items[0]["title"] == "Fed holds rates steady"
        assert items[0]["source"] == "openbb/news/Reuters"


#  2. Tool Resolution 

class TestToolResolution:
    """Test that collector preset resolves correctly."""

    def test_collector_preset_exists(self):
        assert "collector" in TOOL_PRESETS
        tools = TOOL_PRESETS["collector"]
        assert "brave_search" in tools
        assert "fetch_reddit" in tools
        assert "fetch_hn" in tools
        assert "fetch_rss" in tools
        assert "fetch_x" in tools
        assert "fetch_openbb" in tools
        assert "memory_read" in tools
        assert "memory_write" in tools

    def test_resolve_collector_preset(self):
        resolved = resolve_tool_names(["collector"])
        # Feed tools should get mcp__feed__ prefix
        assert "mcp__feed__brave_search" in resolved
        assert "mcp__feed__fetch_reddit" in resolved
        assert "mcp__feed__fetch_hn" in resolved
        assert "mcp__feed__fetch_rss" in resolved
        assert "mcp__feed__fetch_x" in resolved
        assert "mcp__feed__fetch_openbb" in resolved
        # Memory tools should get mcp__mem__ prefix
        assert "mcp__mem__memory_read" in resolved
        assert "mcp__mem__memory_write" in resolved
        assert "mcp__mem__memory_list" in resolved

    def test_resolve_individual_feed_tools(self):
        resolved = resolve_tool_names(["brave_search", "fetch_reddit"])
        assert resolved == ["mcp__feed__brave_search", "mcp__feed__fetch_reddit"]

    def test_resolve_openbb_tool(self):
        resolved = resolve_tool_names(["fetch_openbb"])
        assert resolved == ["mcp__feed__fetch_openbb"]

    def test_feed_tool_names_constant(self):
        assert FEED_TOOL_NAMES == {
            "brave_search", "fetch_rss", "fetch_reddit", "fetch_hn", "fetch_x", "fetch_openbb",
            "fetch_polymarket", "fetch_kalshi", "fetch_metaculus",
        }


#  3. Memory Integration 

class TestFeedInjection:
    """Test that feed.json gets injected into system prompts."""

    def _setup_memory(self, tmpdir: Path):
        """Create a minimal .companest directory with info-collection feed data."""
        # Create info-collection team structure
        team_dir = tmpdir / "teams" / "info-collection"
        mem_dir = team_dir / "memory"
        mem_dir.mkdir(parents=True)
        (team_dir / "team.md").write_text("# Team: info-collection\n- role: info-collection\n")

        # Create a business team to test injection into
        biz_dir = tmpdir / "teams" / "engineering"
        biz_pis = biz_dir / "pis" / "dev"
        biz_pis.mkdir(parents=True)
        (biz_dir / "team.md").write_text("# Team: engineering\n- role: engineering\n")
        (biz_pis / "soul.md").write_text("I am a dev Pi.\n")

        return MemoryManager(str(tmpdir))

    def test_feed_injected_into_business_team(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))

            # Write feed.json
            feed_data = {
                "updated_at": "2026-02-15T10:30:00Z",
                "items": [
                    {"title": "GPT-5 released", "source": "r/LocalLLaMA"},
                    {"title": "Claude 4 announced", "source": "hn"},
                    {"title": "New open-source model", "source": "brave"},
                ],
            }
            mm.write_team_memory("info-collection", "feed.json", feed_data)

            # Build system prompt for business team
            prompt = mm.build_system_prompt("engineering", "dev")
            assert "Recent Feed" in prompt
            assert "GPT-5 released" in prompt
            assert "Claude 4 announced" in prompt
            assert "New open-source model" in prompt
            assert "[r/LocalLLaMA]" in prompt
            assert "[hn]" in prompt
            assert "2026-02-15T10:30:00Z" in prompt

    def test_feed_not_injected_into_info_collection_team(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))

            # Create Pi soul for info-collection team
            pi_dir = Path(tmpdir) / "teams" / "info-collection" / "pis" / "collector"
            pi_dir.mkdir(parents=True)
            (pi_dir / "soul.md").write_text("I am the collector.\n")

            # Write feed.json
            feed_data = {
                "updated_at": "2026-02-15T10:30:00Z",
                "items": [{"title": "Test headline", "source": "hn"}],
            }
            mm.write_team_memory("info-collection", "feed.json", feed_data)

            # Build prompt for info-collection team itself  should NOT include feed
            prompt = mm.build_system_prompt("info-collection", "collector")
            assert "Recent Feed" not in prompt

    def test_no_feed_no_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))
            prompt = mm.build_system_prompt("engineering", "dev")
            assert "Recent Feed" not in prompt

    def test_empty_feed_items_no_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))
            mm.write_team_memory("info-collection", "feed.json", {"items": []})
            prompt = mm.build_system_prompt("engineering", "dev")
            assert "Recent Feed" not in prompt

    def test_malformed_feed_no_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))
            mm.write_team_memory("info-collection", "feed.json", "not a dict")
            prompt = mm.build_system_prompt("engineering", "dev")
            assert "Recent Feed" not in prompt

    def test_feed_max_items_capped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = self._setup_memory(Path(tmpdir))
            items = [{"title": f"Item {i}", "source": "hn"} for i in range(30)]
            mm.write_team_memory("info-collection", "feed.json", {
                "updated_at": "now",
                "items": items,
            })
            prompt = mm.build_system_prompt("engineering", "dev")
            # Max 20 items (MemoryManager._MAX_FEED_ITEMS)
            assert "Item 0" in prompt
            assert "Item 19" in prompt
            assert "Item 20" not in prompt


#  4. Orchestrator Wiring 

class TestOrchestratorInfoCollection:
    """Test that orchestrator wires info-collection correctly."""

    def test_info_collection_registered_as_meta_team(self):
        """Verify info-collection team is registered as always-on meta team."""
        from companest.team import TeamRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            # Create info-collection team
            teams_dir = tmpdir / "teams"
            team_dir = teams_dir / "info-collection"
            mem_dir = team_dir / "memory"
            mem_dir.mkdir(parents=True)
            (team_dir / "team.md").write_text(
                "# Team: info-collection\n"
                "- role: info-collection\n"
                "- lead_pi: collector\n"
                "- always_on: true\n"
                "- enabled: true\n"
                "\n"
                "#### Pi: collector\n"
                "- model: claude-haiku-4-5-20251001\n"
                "- tools: collector\n"
                "- max_turns: 15\n"
            )
            pi_dir = team_dir / "pis" / "collector"
            pi_dir.mkdir(parents=True)
            (pi_dir / "soul.md").write_text("I collect info.\n")

            mm = MemoryManager(str(tmpdir))
            registry = TeamRegistry(
                base_path=str(teams_dir),
                memory=mm,
            )
            registry.scan_configs()

            # Check that info-collection is a meta team
            assert "info-collection" in registry.list_meta_teams()
            assert "info-collection" in registry.list_teams()

    @pytest.mark.asyncio
    async def test_run_enrichment_cycle_calls_run_team(self):
        """Verify run_enrichment_cycle calls run_team with correct args."""
        from companest.background import BackgroundManager

        run_team = AsyncMock(return_value="collected stuff")
        team_registry = MagicMock()
        team_registry.list_teams.return_value = ["info-collection"]

        bg = BackgroundManager(
            run_team_fn=run_team,
            run_auto_fn=AsyncMock(),
            team_registry=team_registry,
            cost_gate=MagicMock(),
            events=MagicMock(),
            scheduler=MagicMock(),
            user_scheduler=MagicMock(),
            enrichment_cycles={},
            info_refresh_team="info-collection",
        )

        prompt = (
            "Run your scheduled collection cycle. "
            "Read watchlist.json for sources. "
            "Fetch from each configured source (brave_search, reddit, hn, rss, x). "
            "Read existing feed.json first, then merge new items. "
            "Deduplicate by URL, drop items older than 2 hours, keep max 50 items. "
            "If any significant trends emerge, update digest.json."
        )
        await bg.run_enrichment_cycle("info-collection", prompt)

        run_team.assert_called_once()
        call_args = run_team.call_args
        assert "info-collection" in call_args.args or call_args.kwargs.get("team_id") == "info-collection"
        assert call_args.kwargs.get("skip_cost_check") is True
        assert call_args.kwargs.get("mode") == "cascade"
        assert "watchlist.json" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_refresh_info_for_task_calls_run_team(self):
        """Verify refresh_info_for_task does a targeted refresh."""
        from companest.background import BackgroundManager

        run_team = AsyncMock(return_value="refreshed")
        team_registry = MagicMock()
        team_registry.list_teams.return_value = ["info-collection", "engineering"]

        bg = BackgroundManager(
            run_team_fn=run_team,
            run_auto_fn=AsyncMock(),
            team_registry=team_registry,
            cost_gate=MagicMock(),
            events=MagicMock(),
            scheduler=MagicMock(),
            user_scheduler=MagicMock(),
            enrichment_cycles={},
            info_refresh_team="info-collection",
        )

        await bg.refresh_info_for_task("What is the latest AI news-")

        run_team.assert_called_once()
        call_args = run_team.call_args
        assert call_args.args[1] == "info-collection"
        assert "latest AI news" in call_args.args[0]
        assert call_args.kwargs.get("skip_cost_check") is True

    @pytest.mark.asyncio
    async def test_refresh_skipped_when_no_team(self):
        """Verify refresh_info_for_task is a no-op without the team."""
        from companest.background import BackgroundManager

        run_team = AsyncMock()
        team_registry = MagicMock()
        team_registry.list_teams.return_value = ["engineering"]

        bg = BackgroundManager(
            run_team_fn=run_team,
            run_auto_fn=AsyncMock(),
            team_registry=team_registry,
            cost_gate=MagicMock(),
            events=MagicMock(),
            scheduler=MagicMock(),
            user_scheduler=MagicMock(),
            enrichment_cycles={},
            info_refresh_team="info-collection",
        )

        await bg.refresh_info_for_task("test task")
        run_team.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_skipped_when_no_team(self):
        """Verify run_enrichment_cycle is a no-op without the team."""
        from companest.background import BackgroundManager

        run_team = AsyncMock()
        team_registry = MagicMock()
        team_registry.list_teams.return_value = ["engineering"]

        bg = BackgroundManager(
            run_team_fn=run_team,
            run_auto_fn=AsyncMock(),
            team_registry=team_registry,
            cost_gate=MagicMock(),
            events=MagicMock(),
            scheduler=MagicMock(),
            user_scheduler=MagicMock(),
            enrichment_cycles={},
            info_refresh_team="info-collection",
        )

        await bg.run_enrichment_cycle("info-collection", "Run collection")
        run_team.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_handles_errors_gracefully(self):
        """Verify run_enrichment_cycle doesn't crash on errors."""
        from companest.background import BackgroundManager

        run_team = AsyncMock(side_effect=Exception("LLM API timeout"))
        team_registry = MagicMock()
        team_registry.list_teams.return_value = ["info-collection"]

        bg = BackgroundManager(
            run_team_fn=run_team,
            run_auto_fn=AsyncMock(),
            team_registry=team_registry,
            cost_gate=MagicMock(),
            events=MagicMock(),
            scheduler=MagicMock(),
            user_scheduler=MagicMock(),
            enrichment_cycles={},
            info_refresh_team="info-collection",
        )

        # Should not raise
        await bg.run_enrichment_cycle("info-collection", "Run collection")


#  5. Watchlist + Team Config 

class TestTeamConfig:
    """Test the info-collection team config and watchlist."""

    def test_watchlist_valid_json(self):
        path = Path("examples/minimal-setup/.companest/teams/info-collection/memory/watchlist.json")
        assert path.exists(), "watchlist.json should exist"
        data = json.loads(path.read_text())
        assert "brave_queries" in data
        assert "reddit" in data
        assert "hn" in data
        assert "openbb" in data
        assert isinstance(data["reddit"], list)
        assert data["hn"]["enabled"] is True
        assert isinstance(data["openbb"]["symbols"], list)
        assert len(data["openbb"]["symbols"]) > 0

    def test_team_md_valid(self):
        path = Path("examples/minimal-setup/.companest/teams/info-collection/team.md")
        assert path.exists(), "team.md should exist"
        content = path.read_text()
        assert "info-collection" in content
        assert "collector" in content
        assert "always_on: true" in content

    def test_collector_soul_valid(self):
        path = Path("examples/minimal-setup/.companest/teams/info-collection/pis/collector/soul.md")
        assert path.exists(), "collector soul.md should exist"
        content = path.read_text()
        assert "watchlist.json" in content
        assert "feed.json" in content
        assert "digest.json" in content
        assert "fetch_openbb" in content
        assert "OpenBB" in content
