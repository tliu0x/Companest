"""
Companest Data Feed Fetchers

Pure async functions for fetching data from external sources.
No LLM calls  just HTTP fetches + parsing.

Supported sources:
- Brave Search (requires BRAVE_API_KEY)
- RSS / Atom feeds
- Reddit public JSON API
- Hacker News API
- X / Twitter (requires X_BEARER_TOKEN)
- OpenBB (requires local OpenBB API server, OPENBB_API_URL env var)

All functions return List[Dict] with normalized item format:
    {"title": ..., "url": ..., "source": ..., "snippet": ..., "timestamp": ...}
"""

import asyncio
import logging
import os
import time
import defusedxml.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from xml.etree.ElementTree import Element as XmlElement

logger = logging.getLogger(__name__)

# Reuse a shared httpx client for connection pooling
_client = None


def _get_client():
    global _client
    if _client is None:
        import httpx
        _client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Companest/1.0 (info-collection)"},
        )
    return _client


async def close_client() -> None:
    """Close the shared httpx client. Call on shutdown to avoid resource leak."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


#  Rate Limiting 

# Per-source minimum interval (seconds) between requests
_RATE_LIMITS: Dict[str, float] = {
    "brave": 1.0,
    "reddit": 2.0,
    "hn": 1.0,
    "rss": 1.0,
    "x": 1.0,
    "openbb": 0.5,
}
_last_request_time: Dict[str, float] = {}


async def _rate_limit(source: str) -> None:
    """Wait if needed to respect per-source rate limit."""
    min_interval = _RATE_LIMITS.get(source, 1.0)
    last = _last_request_time.get(source, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < min_interval:
        await asyncio.sleep(min_interval - elapsed)
    _last_request_time[source] = time.monotonic()


#  Result Cache 

_CACHE_TTL = 300  # 5 minutes
_cache: Dict[str, Any] = {}  # key -> (timestamp, result)


def _cache_key(func_name: str, *args: Any, **kwargs: Any) -> str:
    """Build a cache key from function name and arguments."""
    parts = [func_name] + [str(a) for a in args]
    parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return "|".join(parts)


def _cache_get(key: str) -> Optional[List[Dict]]:
    """Return cached result if still valid, else None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return result


def _cache_set(key: str, result: List[Dict]) -> None:
    """Store result in cache."""
    _cache[key] = (time.monotonic(), result)


#  Brave Search 

async def brave_search(
    query: str,
    count: int = 10,
    freshness: str = "",
    api_key: Optional[str] = None,
) -> List[Dict]:
    """
    Search via Brave Search API.

    Args:
        query: Search query string
        count: Number of results (max 20)
        freshness: Time filter  "" (any), "pd" (past day), "pw" (past week), "pm" (past month)
        api_key: Brave API key (falls back to BRAVE_API_KEY env var)

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp"}
    """
    key = api_key or os.getenv("BRAVE_API_KEY", "")
    if not key:
        return [{"error": "BRAVE_API_KEY not set"}]

    ck = _cache_key("brave_search", query, count, freshness)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    params = {
        "q": query,
        "count": min(count, 20),
    }
    if freshness:
        params["freshness"] = freshness

    await _rate_limit("brave")
    try:
        client = _get_client()
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Brave Search failed: {e}")
        return [{"error": str(e)}]

    items = []
    for result in data.get("web", {}).get("results", []):
        items.append({
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "source": "brave",
            "snippet": result.get("description", ""),
            "timestamp": result.get("age", ""),
        })
    _cache_set(ck, items)
    return items


#  RSS / Atom 

async def fetch_rss(url: str, limit: int = 15) -> List[Dict]:
    """
    Fetch and parse an RSS or Atom feed.

    Args:
        url: Feed URL (RSS 2.0 or Atom)
        limit: Max items to return

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp"}
    """
    ck = _cache_key("fetch_rss", url, limit)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    await _rate_limit("rss")
    try:
        client = _get_client()
        resp = await client.get(url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:
        logger.error(f"RSS fetch failed ({url}): {e}")
        return [{"error": str(e)}]

    items = []

    # RSS 2.0
    for item in root.findall(".//item")[:limit]:
        items.append({
            "title": _xml_text(item, "title"),
            "url": _xml_text(item, "link"),
            "source": "rss",
            "snippet": _xml_text(item, "description", max_len=300),
            "timestamp": _xml_text(item, "pubDate"),
        })

    # Atom (if no RSS items found)
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limit]:
            link_el = entry.find("atom:link", ns)
            items.append({
                "title": _xml_text(entry, "atom:title", ns=ns),
                "url": link_el.get("href", "") if link_el is not None else "",
                "source": "rss",
                "snippet": _xml_text(entry, "atom:summary", ns=ns, max_len=300),
                "timestamp": _xml_text(entry, "atom:updated", ns=ns),
            })

    _cache_set(ck, items)
    return items


def _xml_text(
    el: XmlElement, tag: str, default: str = "",
    ns: Optional[Dict] = None, max_len: int = 0,
) -> str:
    """Extract text from an XML element, handling namespaces."""
    child = el.find(tag, ns) if ns else el.find(tag)
    if child is None or child.text is None:
        return default
    text = child.text.strip()
    # Strip HTML tags from descriptions
    if "<" in text:
        import re
        text = re.sub(r"<[^>]+>", "", text)
    if max_len and len(text) > max_len:
        text = text[:max_len] + "..."
    return text


#  Reddit 

async def fetch_reddit(
    subreddit: str,
    sort: str = "hot",
    limit: int = 10,
) -> List[Dict]:
    """
    Fetch Reddit posts via public JSON API (no auth required).

    Args:
        subreddit: Subreddit name (without r/ prefix)
        sort: "hot", "new", "top", "rising"
        limit: Number of posts (max 25)

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp", "score", "comments"}
    """
    ck = _cache_key("fetch_reddit", subreddit, sort, limit)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    await _rate_limit("reddit")
    try:
        client = _get_client()
        resp = await client.get(
            url,
            params={"limit": min(limit, 25), "raw_json": 1},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Reddit fetch failed (r/{subreddit}): {e}")
        return [{"error": str(e)}]

    items = []
    for post in data.get("data", {}).get("children", []):
        d = post.get("data", {})
        # Convert Unix timestamp
        created = d.get("created_utc", 0)
        ts = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else ""

        items.append({
            "title": d.get("title", ""),
            "url": f"https://reddit.com{d.get('permalink', '')}",
            "source": f"r/{subreddit}",
            "snippet": (d.get("selftext", "") or "")[:300],
            "timestamp": ts,
            "score": d.get("score", 0),
            "comments": d.get("num_comments", 0),
        })
    _cache_set(ck, items)
    return items


#  Hacker News 

async def fetch_hn(
    story_type: str = "top",
    limit: int = 15,
) -> List[Dict]:
    """
    Fetch Hacker News stories via official API.

    Args:
        story_type: "top", "new", "best", "ask", "show"
        limit: Number of stories to fetch (max 30)

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp", "score", "comments"}
    """
    ck = _cache_key("fetch_hn", story_type, limit)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    base = "https://hacker-news.firebaseio.com/v0"
    await _rate_limit("hn")
    try:
        client = _get_client()

        # Get story IDs
        resp = await client.get(f"{base}/{story_type}stories.json")
        resp.raise_for_status()
        story_ids = resp.json()[:min(limit, 30)]

        # Fetch each story (in parallel-ish via gather)
        async def _fetch_one(sid):
            r = await client.get(f"{base}/item/{sid}.json")
            return r.json() if r.status_code == 200 else None

        stories = await asyncio.gather(*[_fetch_one(sid) for sid in story_ids])

    except Exception as e:
        logger.error(f"HN fetch failed: {e}")
        return [{"error": str(e)}]

    items = []
    for story in stories:
        if not story:
            continue
        ts = story.get("time", 0)
        items.append({
            "title": story.get("title", ""),
            "url": story.get("url", f"https://news.ycombinator.com/item?id={story.get('id', '')}"),
            "source": "hn",
            "snippet": "",
            "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
            "score": story.get("score", 0),
            "comments": story.get("descendants", 0),
        })
    _cache_set(ck, items)
    return items


#  X / Twitter 

async def fetch_x(
    username: str,
    limit: int = 10,
    bearer_token: Optional[str] = None,
) -> List[Dict]:
    """
    Fetch X/Twitter user timeline via API v2.

    Requires X_BEARER_TOKEN env var or explicit bearer_token.
    Returns empty list with error message if not configured.

    Args:
        username: X handle (without @)
        limit: Number of tweets (max 100)
        bearer_token: Bearer token (falls back to X_BEARER_TOKEN env var)

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp"}
    """
    token = bearer_token or os.getenv("X_BEARER_TOKEN", "")
    if not token:
        return [{"error": "X_BEARER_TOKEN not set  X feed disabled"}]

    ck = _cache_key("fetch_x", username, limit)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    await _rate_limit("x")
    try:
        client = _get_client()

        # Step 1: Resolve username to user ID
        user_resp = await client.get(
            f"https://api.x.com/2/users/by/username/{username}",
            headers={"Authorization": f"Bearer {token}"},
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()
        user_id = user_data.get("data", {}).get("id")
        if not user_id:
            return [{"error": f"X user not found: {username}"}]

        # Step 2: Get user tweets
        tweets_resp = await client.get(
            f"https://api.x.com/2/users/{user_id}/tweets",
            params={
                "max_results": min(limit, 100),
                "tweet.fields": "created_at,text,public_metrics",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        tweets_resp.raise_for_status()
        tweets_data = tweets_resp.json()

    except Exception as e:
        logger.error(f"X fetch failed (@{username}): {e}")
        return [{"error": str(e)}]

    items = []
    for tweet in tweets_data.get("data", []):
        text = tweet.get("text", "")
        items.append({
            "title": text[:100] + ("..." if len(text) > 100 else ""),
            "url": f"https://x.com/{username}/status/{tweet.get('id', '')}",
            "source": f"x/@{username}",
            "snippet": text,
            "timestamp": tweet.get("created_at", ""),
        })
    _cache_set(ck, items)
    return items


#  OpenBB 

_OPENBB_ENDPOINTS = {
    "quote": "/api/v1/equity/price/quote",
    "historical": "/api/v1/equity/price/historical",
    "news": "/api/v1/news/world",
    "economy": "/api/v1/economy/indicators",
    "crypto": "/api/v1/crypto/price/historical",
}


async def fetch_openbb(
    symbols: str,
    data_type: str = "quote",
    provider: str = "yfinance",
    base_url: Optional[str] = None,
) -> List[Dict]:
    """
    Fetch financial data from a local OpenBB API server.

    Requires a running OpenBB API (``openbb-api`` or uvicorn).
    Server URL defaults to OPENBB_API_URL env var or http://127.0.0.1:6900.

    Args:
        symbols: Comma-separated ticker symbols (e.g. "AAPL,MSFT,NVDA")
        data_type: One of "quote", "historical", "news", "economy", "crypto"
        provider: Data provider backend (default "yfinance")
        base_url: Override server URL

    Returns:
        List of {"title", "url", "source", "snippet", "timestamp", ...}
    """
    url = base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")
    endpoint = _OPENBB_ENDPOINTS.get(data_type)
    if not endpoint:
        return [{"error": f"Unknown data_type: {data_type}. Use: {', '.join(_OPENBB_ENDPOINTS)}"}]

    ck = _cache_key("fetch_openbb", symbols, data_type, provider)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    params: Dict[str, Any] = {"provider": provider}
    if data_type != "news":
        params["symbol"] = symbols

    await _rate_limit("openbb")
    try:
        client = _get_client()
        resp = await client.get(f"{url}{endpoint}", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"OpenBB fetch failed ({data_type}, {symbols}): {e}")
        return [{"error": f"OpenBB API unavailable: {e}"}]

    results = data.get("results", [])
    if not isinstance(results, list):
        results = [results] if results else []

    now = datetime.now(timezone.utc).isoformat()

    if data_type == "quote":
        items = _normalize_quotes(results, now)
    elif data_type == "news":
        items = _normalize_news(results)
    elif data_type == "economy":
        items = _normalize_economy(results, now)
    elif data_type in ("historical", "crypto"):
        items = _normalize_historical(results, symbols, data_type, now)
    else:
        return [{"error": f"Unhandled data_type: {data_type}"}]
    _cache_set(ck, items)
    return items


def _normalize_quotes(results: List[Dict], now: str) -> List[Dict]:
    """Normalize OpenBB equity quote results to feed items."""
    items = []
    for q in results:
        symbol = q.get("symbol", "-")
        price = q.get("last_price") or q.get("close") or q.get("price", "N/A")
        change = q.get("change_percent") or q.get("percent_change", 0)
        change_str = f"{change:+.2f}%" if isinstance(change, (int, float)) else str(change)

        parts = []
        if q.get("volume"):
            parts.append(f"Vol: {q['volume']:,.0f}")
        if q.get("market_cap"):
            parts.append(f"MCap: {_fmt_large_num(q['market_cap'])}")
        if q.get("pe_ratio"):
            parts.append(f"P/E: {q['pe_ratio']:.1f}")

        items.append({
            "title": f"{symbol}: ${price} ({change_str})",
            "url": f"https://finance.yahoo.com/quote/{symbol}",
            "source": "openbb/quote",
            "snippet": ", ".join(parts) if parts else f"{symbol} latest quote",
            "timestamp": now,
        })
    return items


def _normalize_news(results: List[Dict]) -> List[Dict]:
    """Normalize OpenBB news results to feed items."""
    items = []
    for n in results:
        items.append({
            "title": n.get("title", ""),
            "url": n.get("url") or n.get("link", ""),
            "source": f"openbb/news/{n.get('source', 'unknown')}",
            "snippet": (n.get("text") or n.get("description") or "")[:300],
            "timestamp": n.get("date") or n.get("published", ""),
        })
    return items


def _normalize_economy(results: List[Dict], now: str) -> List[Dict]:
    """Normalize OpenBB economy indicator results to feed items."""
    items = []
    for e in results:
        name = e.get("title") or e.get("name") or e.get("symbol", "-")
        value = e.get("value", "N/A")
        items.append({
            "title": f"{name}: {value}",
            "url": "",
            "source": "openbb/economy",
            "snippet": f"Country: {e.get('country', 'N/A')}, Unit: {e.get('unit', 'N/A')}",
            "timestamp": e.get("date") or now,
        })
    return items


def _normalize_historical(
    results: List[Dict], symbols: str, data_type: str, now: str,
) -> List[Dict]:
    """Normalize OpenBB historical price results to a summary feed item."""
    if not results:
        return []
    # Summarize: latest data point from the series
    latest = results[-1] if results else {}
    label = symbols.split(",")[0]
    source_tag = "openbb/crypto" if data_type == "crypto" else "openbb/historical"
    o, h, l, c = latest.get("open", ""), latest.get("high", ""), latest.get("low", ""), latest.get("close", "")
    return [{
        "title": f"{label} OHLC: O={o} H={h} L={l} C={c}",
        "url": f"https://finance.yahoo.com/quote/{label}",
        "source": source_tag,
        "snippet": f"Volume: {latest.get('volume', 'N/A')}, Date: {latest.get('date', 'N/A')}",
        "timestamp": latest.get("date") or now,
    }]


#  Multi-Source Aggregation 

_SOURCE_FETCHERS = {
    "brave": lambda q, n: brave_search(q, count=n),
    "reddit": lambda q, n: fetch_reddit(q, limit=n),
    "hn": lambda q, n: fetch_hn(limit=n),
    "rss": lambda q, n: fetch_rss(q, limit=n),
}


async def fetch_multi_source(
    query: str,
    sources: Optional[List[str]] = None,
    per_source_limit: int = 5,
) -> List[Dict]:
    """
    Fetch from multiple sources in parallel and merge results.

    Args:
        query: Search query (used as subreddit for Reddit, URL for RSS).
        sources: List of source names. Default: ["brave", "hn", "reddit"].
        per_source_limit: Max items per source.

    Returns:
        Merged list of feed items from all sources, errors included.
    """
    if sources is None:
        sources = ["brave", "hn", "reddit"]

    tasks = []
    source_names = []
    for src in sources:
        fetcher = _SOURCE_FETCHERS.get(src)
        if fetcher:
            tasks.append(fetcher(query, per_source_limit))
            source_names.append(src)
        else:
            logger.warning(f"Unknown source '{src}' in fetch_multi_source")

    if not tasks:
        return [{"error": "No valid sources specified"}]

    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    merged: List[Dict] = []
    for src_name, result in zip(source_names, results_list):
        if isinstance(result, Exception):
            merged.append({"error": f"{src_name}: {result}", "source": src_name})
        elif isinstance(result, list):
            merged.extend(result)

    return merged


def _fmt_large_num(n) -> str:
    """Format a large number with T/B/M suffix."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1e12:
        return f"${n / 1e12:.1f}T"
    if n >= 1e9:
        return f"${n / 1e9:.1f}B"
    if n >= 1e6:
        return f"${n / 1e6:.1f}M"
    return f"${n:,.0f}"
