"""Companest Data Feed Fetchers"""

import asyncio
import logging
import os
import re
import time
import defusedxml.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# Per-source minimum interval (seconds) between requests
_RATE_LIMITS: Dict[str, float] = {
    "brave": 1.0,
    "reddit": 2.0,
    "hn": 1.0,
    "rss": 1.0,
    "x": 1.0,
    "openbb": 0.5,
    "polymarket": 1.0,
    "kalshi": 1.0,
    "metaculus": 1.0,
}
_last_request_time: Dict[str, float] = {}


async def _rate_limit(source: str) -> None:
    min_interval = _RATE_LIMITS.get(source, 1.0)
    elapsed = time.monotonic() - _last_request_time.get(source, 0.0)
    if elapsed < min_interval:
        await asyncio.sleep(min_interval - elapsed)
    _last_request_time[source] = time.monotonic()


_CACHE_TTL = 300
_cache: Dict[str, Any] = {}  # key -> (timestamp, result)


def _cache_key(func_name: str, *args: Any, **kwargs: Any) -> str:
    parts = [func_name] + [str(a) for a in args]
    parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return "|".join(parts)


def _cache_get(key: str) -> Optional[List[Dict]]:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return result


def _cache_set(key: str, result: List[Dict]) -> None:
    _cache[key] = (time.monotonic(), result)


async def _get_json(client, url: str, **kwargs: Any) -> Any:
    resp = await client.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()


async def _fetch(ck: str, source: str, failure: str, request, parse, error=str) -> List[Dict]:
    """Cached, rate-limited request + parse. Only request failures become error items; they are not cached."""
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    await _rate_limit(source)
    try:
        data = await request(_get_client())
    except Exception as e:
        logger.error(f"{failure}: {e}")
        return [{"error": error(e)}]
    items = parse(data)
    _cache_set(ck, items)
    return items


async def brave_search(
    query: str,
    count: int = 10,
    freshness: str = "",
    api_key: Optional[str] = None,
) -> List[Dict]:
    key = api_key or os.getenv("BRAVE_API_KEY", "")
    if not key:
        return [{"error": "BRAVE_API_KEY not set"}]

    params = {"q": query, "count": min(count, 20)}
    if freshness:
        params["freshness"] = freshness

    return await _fetch(
        _cache_key("brave_search", query, count, freshness), "brave", "Brave Search failed",
        lambda client: _get_json(
            client, "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        ),
        lambda data: [{
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "source": "brave",
            "snippet": result.get("description", ""),
            "timestamp": result.get("age", ""),
        } for result in data.get("web", {}).get("results", [])],
    )


async def fetch_rss(url: str, limit: int = 15) -> List[Dict]:
    """Fetch and parse an RSS 2.0 or Atom feed."""
    async def request(client):
        resp = await client.get(url)
        resp.raise_for_status()
        return ET.fromstring(resp.text)

    def parse(root):
        items = [{
            "title": _xml_text(item, "title"),
            "url": _xml_text(item, "link"),
            "source": "rss",
            "snippet": _xml_text(item, "description", max_len=300),
            "timestamp": _xml_text(item, "pubDate"),
        } for item in root.findall(".//item")[:limit]]
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
        return items

    return await _fetch(_cache_key("fetch_rss", url, limit), "rss", f"RSS fetch failed ({url})", request, parse)


def _xml_text(el, tag: str, ns: Optional[Dict] = None, max_len: int = 0) -> str:
    child = el.find(tag, ns)
    if child is None or child.text is None:
        return ""
    text = re.sub(r"<[^>]+>", "", child.text.strip())
    if max_len and len(text) > max_len:
        text = text[:max_len] + "..."
    return text


async def fetch_reddit(
    subreddit: str,
    sort: str = "hot",
    limit: int = 10,
) -> List[Dict]:
    """Fetch Reddit posts via public JSON API (no auth required)."""
    def parse(data):
        items = []
        for post in data.get("data", {}).get("children", []):
            d = post.get("data", {})
            created = d.get("created_utc", 0)
            items.append({
                "title": d.get("title", ""),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "source": f"r/{subreddit}",
                "snippet": (d.get("selftext", "") or "")[:300],
                "timestamp": datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else "",
                "score": d.get("score", 0),
                "comments": d.get("num_comments", 0),
            })
        return items

    return await _fetch(
        _cache_key("fetch_reddit", subreddit, sort, limit), "reddit", f"Reddit fetch failed (r/{subreddit})",
        lambda client: _get_json(
            client, f"https://www.reddit.com/r/{subreddit}/{sort}.json",
            params={"limit": min(limit, 25), "raw_json": 1},
        ),
        parse,
    )


async def fetch_hn(
    story_type: str = "top",
    limit: int = 15,
) -> List[Dict]:
    base = "https://hacker-news.firebaseio.com/v0"

    async def request(client):
        story_ids = (await _get_json(client, f"{base}/{story_type}stories.json"))[:min(limit, 30)]

        async def _fetch_one(sid):
            r = await client.get(f"{base}/item/{sid}.json")
            return r.json() if r.status_code == 200 else None

        return await asyncio.gather(*[_fetch_one(sid) for sid in story_ids])

    def parse(stories):
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
        return items

    return await _fetch(_cache_key("fetch_hn", story_type, limit), "hn", "HN fetch failed", request, parse)


async def fetch_x(
    username: str,
    limit: int = 10,
    bearer_token: Optional[str] = None,
) -> List[Dict]:
    """Fetch X/Twitter user timeline via API v2."""
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
        headers = {"Authorization": f"Bearer {token}"}
        user_data = await _get_json(client, f"https://api.x.com/2/users/by/username/{username}", headers=headers)
        user_id = user_data.get("data", {}).get("id")
        if not user_id:
            return [{"error": f"X user not found: {username}"}]
        tweets_data = await _get_json(
            client, f"https://api.x.com/2/users/{user_id}/tweets",
            params={
                "max_results": min(limit, 100),
                "tweet.fields": "created_at,text,public_metrics",
            },
            headers=headers,
        )
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
    """Fetch financial data from a local OpenBB API server."""
    url = base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")
    endpoint = _OPENBB_ENDPOINTS.get(data_type)
    if not endpoint:
        return [{"error": f"Unknown data_type: {data_type}. Use: {', '.join(_OPENBB_ENDPOINTS)}"}]

    params: Dict[str, Any] = {"provider": provider}
    if data_type != "news":
        params["symbol"] = symbols

    def parse(data):
        results = data.get("results", [])
        if not isinstance(results, list):
            results = [results] if results else []
        now = datetime.now(timezone.utc).isoformat()
        if data_type == "quote":
            return _normalize_quotes(results, now)
        if data_type == "news":
            return _normalize_news(results)
        if data_type == "economy":
            return _normalize_economy(results, now)
        return _normalize_historical(results, symbols, data_type, now)

    return await _fetch(
        _cache_key("fetch_openbb", symbols, data_type, provider), "openbb",
        f"OpenBB fetch failed ({data_type}, {symbols})",
        lambda client: _get_json(client, f"{url}{endpoint}", params=params),
        parse,
        lambda e: f"OpenBB API unavailable: {e}",
    )


def _normalize_quotes(results: List[Dict], now: str) -> List[Dict]:
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
    return [{
        "title": n.get("title", ""),
        "url": n.get("url") or n.get("link", ""),
        "source": f"openbb/news/{n.get('source', 'unknown')}",
        "snippet": (n.get("text") or n.get("description") or "")[:300],
        "timestamp": n.get("date") or n.get("published", ""),
    } for n in results]


def _normalize_economy(results: List[Dict], now: str) -> List[Dict]:
    return [{
        "title": f"{e.get('title') or e.get('name') or e.get('symbol', '-')}: {e.get('value', 'N/A')}",
        "url": "",
        "source": "openbb/economy",
        "snippet": f"Country: {e.get('country', 'N/A')}, Unit: {e.get('unit', 'N/A')}",
        "timestamp": e.get("date") or now,
    } for e in results]


def _normalize_historical(
    results: List[Dict], symbols: str, data_type: str, now: str,
) -> List[Dict]:
    """Summarize a price series as one item for its latest data point."""
    if not results:
        return []
    latest = results[-1]
    label = symbols.split(",")[0]
    o, h, l, c = latest.get("open", ""), latest.get("high", ""), latest.get("low", ""), latest.get("close", "")
    return [{
        "title": f"{label} OHLC: O={o} H={h} L={l} C={c}",
        "url": f"https://finance.yahoo.com/quote/{label}",
        "source": "openbb/crypto" if data_type == "crypto" else "openbb/historical",
        "snippet": f"Volume: {latest.get('volume', 'N/A')}, Date: {latest.get('date', 'N/A')}",
        "timestamp": latest.get("date") or now,
    }]


async def fetch_polymarket(
    query: str = "",
    limit: int = 10,
    active_only: bool = True,
) -> List[Dict]:
    params: Dict[str, Any] = {"limit": min(limit, 50)}
    if active_only:
        params["active"] = "true"

    def parse(data):
        markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        items = []
        query_lower = query.lower()
        for m in markets:
            question = m.get("question", "") or m.get("title", "")
            if query and query_lower not in question.lower():
                continue
            slug = m.get("slug", m.get("condition_id", ""))
            items.append({
                "title": question,
                "url": f"https://polymarket.com/event/{slug}" if slug else "",
                "source": "polymarket",
                "snippet": (m.get("description", "") or "")[:300],
                "timestamp": m.get("end_date_iso", m.get("created_at", "")),
                "probability": m.get("outcomePrices", m.get("last_trade_price")),
                "volume": m.get("volume", m.get("volumeNum")),
            })
            if len(items) >= limit:
                break
        return items

    return await _fetch(
        _cache_key("fetch_polymarket", query, limit, active_only), "polymarket", "Polymarket fetch failed",
        lambda client: _get_json(client, "https://clob.polymarket.com/markets", params=params),
        parse,
    )


async def fetch_kalshi(
    query: str = "",
    limit: int = 10,
    status: str = "open",
) -> List[Dict]:
    params: Dict[str, Any] = {"limit": min(limit, 50), "status": status}

    def parse(data):
        items = []
        query_lower = query.lower()
        for m in data.get("markets", []):
            title = m.get("title", "") or m.get("subtitle", "")
            if query and query_lower not in title.lower():
                continue
            ticker = m.get("ticker", "")
            items.append({
                "title": title,
                "url": f"https://kalshi.com/markets/{ticker}" if ticker else "",
                "source": "kalshi",
                "snippet": (m.get("rules_primary", "") or m.get("subtitle", ""))[:300],
                "timestamp": m.get("close_time", m.get("expiration_time", "")),
                "yes_price": m.get("yes_ask", m.get("last_price")),
                "volume": m.get("volume", m.get("open_interest")),
            })
            if len(items) >= limit:
                break
        return items

    return await _fetch(
        _cache_key("fetch_kalshi", query, limit, status), "kalshi", "Kalshi fetch failed",
        lambda client: _get_json(client, "https://api.elections.kalshi.com/trade-api/v2/markets", params=params),
        parse,
    )


async def fetch_metaculus(
    query: str = "",
    limit: int = 10,
    order_by: str = "-activity",
) -> List[Dict]:
    params: Dict[str, Any] = {"limit": min(limit, 50), "order_by": order_by}
    if query:
        params["search"] = query

    def parse(data):
        items = []
        for q in data.get("results", []):
            qid = q.get("id", "")
            prediction = q.get("community_prediction", {})
            items.append({
                "title": q.get("title", ""),
                "url": f"https://www.metaculus.com/questions/{qid}/" if qid else "",
                "source": "metaculus",
                "snippet": (q.get("description", "") or "")[:300],
                "timestamp": q.get("publish_time", q.get("created_time", "")),
                "prediction": prediction.get("full", {}).get("q2") if isinstance(prediction, dict) else None,
                "num_predictions": q.get("number_of_predictions", 0),
            })
            if len(items) >= limit:
                break
        return items

    return await _fetch(
        _cache_key("fetch_metaculus", query, limit, order_by), "metaculus", "Metaculus fetch failed",
        lambda client: _get_json(client, "https://www.metaculus.com/api2/questions/", params=params),
        parse,
    )


def _fmt_large_num(n) -> str:
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
