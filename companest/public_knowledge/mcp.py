"""
MCP tool definitions for Public Knowledge.

Defines search_public_knowledge and read_public_knowledge tools
following the ToolDefinition pattern from companest.tools.

These function definitions will be registered in Phase 2.
"""

import json
import logging
import os
from typing import List

logger = logging.getLogger(__name__)


def create_public_knowledge_tool_defs(
    bucket: str = "",
    prefix: str = "companest-public-knowledge/",
    region: str = "us-east-1",
    endpoint_url: str = "",
) -> List:
    """
    Create ToolDefinitions for public knowledge operations.

    Returns a list of ToolDefinition instances (imported from companest.tools).
    """
    from ..tools import ToolDefinition
    from .index_store import IndexStore
    from .models import SearchQuery
    from .s3_store import PublicKnowledgeS3Store
    from .search import search as search_index

    _endpoint = endpoint_url or None
    _store = PublicKnowledgeS3Store(
        bucket=bucket, prefix=prefix, region=region, endpoint_url=_endpoint,
    )
    _index_store = IndexStore(
        bucket=bucket, prefix=prefix, region=region, endpoint_url=_endpoint,
    )

    async def search_public_knowledge(args: dict) -> str:
        """Search the public knowledge index."""
        tags_raw = args.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None

        query = SearchQuery(
            tags=tags,
            source_type=args.get("source_type") or None,
            fresh_only=bool(args.get("fresh_only", False)),
            keyword=args.get("keyword") or None,
            limit=int(args.get("limit", 20)),
        )

        entries = _index_store.load_index()
        results = search_index(query, entries)

        return json.dumps(
            [r.model_dump(mode="json") for r in results],
            ensure_ascii=False,
            indent=2,
        )

    async def read_public_knowledge(args: dict) -> str:
        """Read a full public knowledge document by ID."""
        doc_id = args["doc_id"]
        doc = _store.get_doc(doc_id)
        if doc is None:
            return json.dumps({"error": f"Document not found: {doc_id}"})
        return doc.model_dump_json(indent=2)

    return [
        ToolDefinition(
            name="search_public_knowledge",
            description=(
                "Search the shared public knowledge base. "
                "Returns index entries matching the query (tags, source_type, keyword). "
                "Use read_public_knowledge to fetch the full document with summary."
            ),
            parameters={
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags to filter by (any match). "
                                   "Prefixes: topic:, market:, entity:, event:, source:",
                    "optional": True,
                },
                "source_type": {
                    "type": "string",
                    "description": "Filter by source type: news, research, filing, social, prediction_market",
                    "optional": True,
                },
                "fresh_only": {
                    "type": "boolean",
                    "description": "If true, only return docs that are still fresh (fresh_until > now)",
                    "optional": True,
                },
                "keyword": {
                    "type": "string",
                    "description": "Case-insensitive keyword search in document title",
                    "optional": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 20, max: 200)",
                    "optional": True,
                },
            },
            handler=search_public_knowledge,
        ),
        ToolDefinition(
            name="read_public_knowledge",
            description=(
                "Read the full content of a public knowledge document by its doc_id. "
                "Returns the complete document including summary."
            ),
            parameters={
                "doc_id": {
                    "type": "string",
                    "description": "The unique document ID (UUID) to retrieve",
                },
            },
            handler=read_public_knowledge,
        ),
    ]
