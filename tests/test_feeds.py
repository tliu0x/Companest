"""Unit tests for companest/knowledge/feeds.py - mock-based (no network)."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companest.knowledge import feeds
from companest.knowledge.feeds import (
    _cache,
    _cache_get,
    _cache_key,
    _last_request_time,
    _rate_limit,
    brave_search,
    close_client,
    fetch_hn,
    fetch_openbb,
    fetch_reddit,
    fetch_rss,
    fetch_x,
)


@pytest.fixture(autouse=True)
def clear_caches():
    _cache.clear()
    _last_request_time.clear()
    yield
    _cache.clear()
    _last_request_time.clear()


class TestCache:
    def test_cache_key_depends_on_arguments(self):
        assert _cache_key("f", "a", "b", x=1) == _cache_key("f", "a", "b", x=1)
        assert _cache_key("f", "a") != _cache_key("f", "b")

    def test_cache_expiry(self):
        _cache["expired"] = (time.monotonic() - 600, [{"old": True}])
        assert _cache_get("expired") is None
        assert "expired" not in _cache


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_rate_limit_delays_second_call(self):
        feeds._RATE_LIMITS["test_rl"] = 0.2
        _last_request_time["test_rl"] = time.monotonic()
        start = time.monotonic()
        await _rate_limit("test_rl")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15


class TestBraveSearch:
    @pytest.mark.asyncio
    async def test_no_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            items = await brave_search("test")
        assert len(items) == 1
        assert "error" in items[0]

    @pytest.mark.asyncio
    async def test_successful_search(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "web": {
                "results": [
                    {"title": "Result 1", "url": "https://example.com", "description": "Desc 1", "age": "2h"},
                    {"title": "Result 2", "url": "https://example.org", "description": "Desc 2", "age": "5h"},
                ]
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await brave_search("test query", api_key="fake-key")

        assert len(items) == 2
        assert items[0]["title"] == "Result 1"
        assert items[0]["source"] == "brave"
        assert items[1]["url"] == "https://example.org"

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"web": {"results": [{"title": "A", "url": "", "description": "", "age": ""}]}}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            r1 = await brave_search("cached", api_key="key")
            r2 = await brave_search("cached", api_key="key")

        assert r1 == r2
        assert mock_client.get.call_count == 1



@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", [
    lambda: brave_search("fail", api_key="key"),
    lambda: fetch_reddit("fail"),
    lambda: fetch_hn("top", 3),
    lambda: fetch_rss("https://bad.example.com/feed"),
    lambda: feeds.fetch_polymarket("test"),
    lambda: feeds.fetch_kalshi("test"),
    lambda: feeds.fetch_metaculus("test"),
], ids=["brave", "reddit", "hn", "rss", "polymarket", "kalshi", "metaculus"])
async def test_http_error_returns_error_item(fetch):
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))

    with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
        items = await fetch()

    assert items == [{"error": "Connection refused"}]


class TestFetchReddit:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "Post 1",
                            "permalink": "/r/test/comments/abc",
                            "selftext": "Body text",
                            "created_utc": 1700000000,
                            "score": 42,
                            "num_comments": 10,
                        }
                    },
                ]
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_reddit("test", "hot", 5)

        assert len(items) == 1
        assert items[0]["title"] == "Post 1"
        assert items[0]["source"] == "r/test"
        assert items[0]["score"] == 42



class TestFetchHN:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        story_resp = MagicMock()
        story_resp.raise_for_status = MagicMock()
        story_resp.json.return_value = [101, 102]

        detail_resp_1 = MagicMock()
        detail_resp_1.status_code = 200
        detail_resp_1.json.return_value = {
            "id": 101, "title": "Story 1", "url": "https://example.com",
            "time": 1700000000, "score": 100, "descendants": 50,
        }
        detail_resp_2 = MagicMock()
        detail_resp_2.status_code = 200
        detail_resp_2.json.return_value = {
            "id": 102, "title": "Story 2",
            "time": 1700001000, "score": 50, "descendants": 20,
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[story_resp, detail_resp_1, detail_resp_2])

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_hn("top", 2)

        assert len(items) == 2
        assert items[0]["title"] == "Story 1"
        assert items[0]["source"] == "hn"
        assert items[1]["score"] == 50



class TestFetchRSS:
    RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <item>
                <title>Article 1</title>
                <link>https://example.com/1</link>
                <description>First article</description>
                <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
            </item>
            <item>
                <title>Article 2</title>
                <link>https://example.com/2</link>
                <description>Second article</description>
                <pubDate>Tue, 02 Jan 2024 00:00:00 GMT</pubDate>
            </item>
        </channel>
    </rss>"""

    @pytest.mark.asyncio
    async def test_parse_rss(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = self.RSS_XML
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_rss("https://example.com/feed", 10)

        assert len(items) == 2
        assert items[0]["title"] == "Article 1"
        assert items[0]["source"] == "rss"
        assert items[1]["url"] == "https://example.com/2"

    ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>Atom Entry</title>
            <link href="https://example.com/atom"/>
            <summary>Atom summary</summary>
            <updated>2024-01-01T00:00:00Z</updated>
        </entry>
    </feed>"""

    @pytest.mark.asyncio
    async def test_parse_atom(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = self.ATOM_XML
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_rss("https://example.com/atom-feed", 10)

        assert len(items) == 1
        assert items[0]["title"] == "Atom Entry"
        assert items[0]["url"] == "https://example.com/atom"



class TestFetchX:
    @pytest.mark.asyncio
    async def test_no_bearer_token(self):
        with patch.dict("os.environ", {}, clear=True):
            items = await fetch_x("testuser")
        assert len(items) == 1
        assert "error" in items[0]

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        user_resp = MagicMock()
        user_resp.raise_for_status = MagicMock()
        user_resp.json.return_value = {"data": {"id": "12345"}}

        tweets_resp = MagicMock()
        tweets_resp.raise_for_status = MagicMock()
        tweets_resp.json.return_value = {
            "data": [
                {"id": "001", "text": "Hello world", "created_at": "2024-01-01T00:00:00Z"},
            ]
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[user_resp, tweets_resp])

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_x("testuser", bearer_token="fake-token")

        assert len(items) == 1
        assert items[0]["source"] == "x/@testuser"
        assert "Hello world" in items[0]["snippet"]


class TestFetchOpenBB:
    @pytest.mark.asyncio
    async def test_bad_data_type(self):
        items = await fetch_openbb("AAPL", "nonexistent")
        assert len(items) == 1
        assert "Unknown data_type" in items[0]["error"]

    @pytest.mark.asyncio
    async def test_successful_quote(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "symbol": "AAPL",
                    "last_price": 245.30,
                    "change_percent": 1.25,
                    "volume": 52_300_000,
                    "market_cap": 3.8e12,
                    "pe_ratio": 31.2,
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await fetch_openbb("AAPL", "quote", base_url="http://localhost:6900")

        assert len(items) == 1
        assert items[0]["title"] == "AAPL: $245.3 (+1.25%)"
        assert items[0]["source"] == "openbb/quote"
        assert "Vol:" in items[0]["snippet"]
        assert "$3.8T" in items[0]["snippet"]
        assert "P/E: 31.2" in items[0]["snippet"]


class TestFetchPolymarket:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {
                "question": "Will AI pass the Turing test by 2030?",
                "slug": "ai-turing-test",
                "description": "Market about AI capabilities",
                "end_date_iso": "2030-12-31T00:00:00Z",
                "outcomePrices": "0.35",
                "volume": 50000,
            },
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await feeds.fetch_polymarket("AI", limit=5)

        assert len(items) == 1
        assert items[0]["source"] == "polymarket"
        assert "Turing" in items[0]["title"]



class TestFetchKalshi:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "markets": [
                {
                    "title": "Fed rate cut in March?",
                    "ticker": "FED-RATE-MAR",
                    "rules_primary": "Will the Fed cut rates?",
                    "close_time": "2026-03-31T00:00:00Z",
                    "yes_ask": 0.65,
                    "volume": 10000,
                },
            ]
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await feeds.fetch_kalshi("Fed", limit=5)

        assert len(items) == 1
        assert items[0]["source"] == "kalshi"
        assert "Fed" in items[0]["title"]



class TestFetchMetaculus:
    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "id": 12345,
                    "title": "When will AGI be achieved?",
                    "description": "Question about artificial general intelligence",
                    "publish_time": "2024-01-01T00:00:00Z",
                    "community_prediction": {"full": {"q2": 0.42}},
                    "number_of_predictions": 500,
                },
            ]
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("companest.knowledge.feeds._get_client", return_value=mock_client):
            items = await feeds.fetch_metaculus("AGI", limit=5)

        assert len(items) == 1
        assert items[0]["source"] == "metaculus"
        assert "AGI" in items[0]["title"]
        assert items[0]["prediction"] == 0.42


class TestCloseClient:
    @pytest.mark.asyncio
    async def test_close_existing_client(self):
        mock_client = AsyncMock()
        feeds._client = mock_client
        await close_client()
        mock_client.aclose.assert_awaited_once()
        assert feeds._client is None
