"""Integration tests for the info-collection system."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from companest.background import BackgroundManager
from companest.knowledge.feeds import _normalize_news, fetch_hn, fetch_openbb, fetch_reddit, fetch_rss
from companest.memory import MemoryManager
from companest.tools import resolve_tool_names


class TestFeedFetchersLive:
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
    async def test_fetch_openbb_no_server(self):
        items = await fetch_openbb("AAPL", "quote", base_url="http://127.0.0.1:19999")
        assert len(items) == 1
        assert "error" in items[0]
        assert "OpenBB API unavailable" in items[0]["error"]

    def test_fetch_openbb_normalize_news(self):
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


class TestToolResolution:
    def test_resolve_collector_preset(self):
        resolved = resolve_tool_names(["collector"])
        assert "brave_search" in resolved
        assert "fetch_reddit" in resolved
        assert "fetch_hn" in resolved
        assert "fetch_rss" in resolved
        assert "fetch_x" in resolved
        assert "fetch_openbb" in resolved
        assert "memory_read" in resolved
        assert "memory_write" in resolved
        assert "memory_list" in resolved

    def test_resolve_individual_feed_tools(self):
        resolved = resolve_tool_names(["brave_search", "fetch_reddit"])
        assert resolved == ["brave_search", "fetch_reddit"]


class TestFeedInjection:
    """feed.json of the info-collection team is injected into other teams' system prompts."""

    @pytest.fixture
    def mm(self, tmp_path):
        team_dir = tmp_path / "teams" / "info-collection"
        (team_dir / "memory").mkdir(parents=True)
        (team_dir / "team.md").write_text("# Team: info-collection\n- role: info-collection\n")

        biz_dir = tmp_path / "teams" / "engineering"
        biz_pis = biz_dir / "pis" / "dev"
        biz_pis.mkdir(parents=True)
        (biz_dir / "team.md").write_text("# Team: engineering\n- role: engineering\n")
        (biz_pis / "soul.md").write_text("I am a dev Pi.\n")

        return MemoryManager(str(tmp_path))

    def test_feed_injected_into_business_team(self, mm):
        feed_data = {
            "updated_at": "2026-02-15T10:30:00Z",
            "items": [
                {"title": "GPT-5 released", "source": "r/LocalLLaMA"},
                {"title": "Claude 4 announced", "source": "hn"},
                {"title": "New open-source model", "source": "brave"},
            ],
        }
        mm.write_team_memory("info-collection", "feed.json", feed_data)

        prompt = mm.build_system_prompt("engineering", "dev")
        assert "Recent Feed" in prompt
        assert "GPT-5 released" in prompt
        assert "Claude 4 announced" in prompt
        assert "New open-source model" in prompt
        assert "[r/LocalLLaMA]" in prompt
        assert "[hn]" in prompt
        assert "2026-02-15T10:30:00Z" in prompt

    def test_feed_not_injected_into_info_collection_team(self, mm, tmp_path):
        pi_dir = tmp_path / "teams" / "info-collection" / "pis" / "collector"
        pi_dir.mkdir(parents=True)
        (pi_dir / "soul.md").write_text("I am the collector.\n")

        feed_data = {
            "updated_at": "2026-02-15T10:30:00Z",
            "items": [{"title": "Test headline", "source": "hn"}],
        }
        mm.write_team_memory("info-collection", "feed.json", feed_data)

        prompt = mm.build_system_prompt("info-collection", "collector")
        assert "Recent Feed" not in prompt

    @pytest.mark.parametrize("feed", [None, {"items": []}, "not a dict"], ids=["missing", "empty", "malformed"])
    def test_unusable_feed_adds_no_section(self, mm, feed):
        if feed is not None:
            mm.write_team_memory("info-collection", "feed.json", feed)
        prompt = mm.build_system_prompt("engineering", "dev")
        assert "Recent Feed" not in prompt

    def test_feed_max_items_capped(self, mm):
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


def _background(run_team, teams):
    team_registry = MagicMock()
    team_registry.list_teams.return_value = teams
    return BackgroundManager(
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


class TestOrchestratorInfoCollection:
    @pytest.mark.asyncio
    async def test_run_enrichment_cycle_calls_run_team(self):
        run_team = AsyncMock(return_value="collected stuff")
        bg = _background(run_team, ["info-collection"])

        await bg.run_enrichment_cycle("info-collection", "Read watchlist.json for sources.")

        run_team.assert_called_once()
        call_args = run_team.call_args
        assert "info-collection" in call_args.args or call_args.kwargs.get("team_id") == "info-collection"
        assert call_args.kwargs.get("skip_cost_check") is True
        assert call_args.kwargs.get("mode") == "cascade"
        assert "watchlist.json" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_refresh_info_for_task_calls_run_team(self):
        run_team = AsyncMock(return_value="refreshed")
        bg = _background(run_team, ["info-collection", "engineering"])

        await bg.refresh_info_for_task("What is the latest AI news-")

        run_team.assert_called_once()
        call_args = run_team.call_args
        assert call_args.args[1] == "info-collection"
        assert "latest AI news" in call_args.args[0]
        assert call_args.kwargs.get("skip_cost_check") is True

    @pytest.mark.asyncio
    async def test_refresh_skipped_when_no_team(self):
        run_team = AsyncMock()
        bg = _background(run_team, ["engineering"])
        await bg.refresh_info_for_task("test task")
        run_team.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_skipped_when_no_team(self):
        run_team = AsyncMock()
        bg = _background(run_team, ["engineering"])
        await bg.run_enrichment_cycle("info-collection", "Run collection")
        run_team.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_handles_errors_gracefully(self):
        run_team = AsyncMock(side_effect=Exception("LLM API timeout"))
        bg = _background(run_team, ["info-collection"])
        await bg.run_enrichment_cycle("info-collection", "Run collection")
        run_team.assert_called_once()
