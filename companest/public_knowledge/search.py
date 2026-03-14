"""
In-memory search over the Public Knowledge index.

Filters index entries by tags, source_type, freshness, and keyword,
then sorts by published_at descending and applies a result limit.
"""

import logging
from datetime import datetime, timezone
from typing import List

from .models import PublicKnowledgeIndexEntry, SearchQuery

logger = logging.getLogger(__name__)


def search(
    query: SearchQuery,
    index: List[PublicKnowledgeIndexEntry],
) -> List[PublicKnowledgeIndexEntry]:
    """
    Filter and sort index entries based on the search query.

    Filters:
    - tags: any match (entry has at least one of the query tags)
    - source_type: exact match
    - fresh_only: fresh_until > now
    - keyword: case-insensitive substring match on title

    Sort: published_at descending
    Limit: query.limit (default 20)
    """
    results = list(index)

    # Filter by tags (any match)
    if query.tags:
        query_tags_lower = {t.lower() for t in query.tags}
        results = [
            e for e in results
            if any(t.lower() in query_tags_lower for t in e.tags)
        ]

    # Filter by source_type (exact match)
    if query.source_type:
        results = [e for e in results if e.source_type == query.source_type]

    # Filter by freshness
    if query.fresh_only:
        now = datetime.now(timezone.utc)
        filtered = []
        for e in results:
            fresh = e.fresh_until
            if fresh.tzinfo is None:
                fresh = fresh.replace(tzinfo=timezone.utc)
            if fresh > now:
                filtered.append(e)
        results = filtered

    # Filter by keyword (case-insensitive title contains)
    if query.keyword:
        kw = query.keyword.lower()
        results = [e for e in results if kw in e.title.lower()]

    # Sort by published_at descending
    results.sort(key=lambda e: e.published_at, reverse=True)

    # Apply limit
    results = results[: query.limit]

    logger.debug(
        "Search returned %d results (query: tags=%s, source_type=%s, keyword=%s)",
        len(results), query.tags, query.source_type, query.keyword,
    )
    return results
