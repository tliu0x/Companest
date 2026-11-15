"""Shared public knowledge."""


import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from ..utils import offload
from .s3 import S3JsonStore

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "companest-public-knowledge/"


class PublicKnowledgeDoc(BaseModel):
    doc_id: str = Field(..., description="Unique document identifier (UUID)")

    @field_validator("doc_id")
    @classmethod
    def validate_doc_id_is_uuid(cls, v: str) -> str:
        UUID(v)
        return v

    source_type: Literal["news", "research", "filing", "social", "prediction_market"] = Field(
        ..., description="Category of the information source"
    )
    title: str = Field(..., min_length=1, max_length=500, description="Document title")
    summary: str = Field(
        ..., min_length=1, max_length=5000, description="Document summary or abstract"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Free-text tags. Use prefixes: topic:, market:, entity:, event:, source:",
    )
    source_url: str = Field(..., description="Original source URL")
    published_at: datetime = Field(
        ..., description="When the source was originally published (ISO8601)"
    )
    fresh_until: datetime = Field(
        ..., description="Document is considered fresh/relevant until this time (ISO8601)"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence score of the information (0=unreliable, 1=highly reliable)",
    )
    visibility: Literal["public", "company_private"] = Field(
        ..., description="public = shared across all companies; company_private = restricted"
    )


class PublicKnowledgeIndexEntry(BaseModel):
    doc_id: str
    source_type: Literal["news", "research", "filing", "social", "prediction_market"]
    title: str = Field(..., min_length=1, max_length=500)
    tags: List[str] = Field(default_factory=list)
    source_url: str
    published_at: datetime
    fresh_until: datetime
    confidence: float = Field(..., ge=0.0, le=1.0)
    visibility: Literal["public", "company_private"]

    @classmethod
    def from_doc(cls, doc: PublicKnowledgeDoc) -> "PublicKnowledgeIndexEntry":
        return cls(**doc.model_dump(exclude={"summary"}))


class SearchQuery(BaseModel):
    tags: Optional[List[str]] = Field(default=None, description="Filter by tags (any match)")
    source_type: Optional[str] = Field(default=None, description="Filter by exact source_type")
    fresh_only: bool = Field(default=False, description="Only return docs where fresh_until > now")
    keyword: Optional[str] = Field(default=None, description="Case-insensitive title search")
    limit: int = Field(default=20, ge=1, le=200, description="Max results to return")


def _is_fresh(entry: "PublicKnowledgeIndexEntry", now: datetime) -> bool:
    fresh = entry.fresh_until
    if fresh.tzinfo is None:
        fresh = fresh.replace(tzinfo=timezone.utc)
    return fresh > now


def _s3_settings_from_env() -> dict:
    return {
        "bucket": os.environ.get("PK_S3_BUCKET", ""),
        "prefix": os.environ.get("PK_S3_PREFIX", DEFAULT_PREFIX),
        "region": os.environ.get("PK_S3_REGION", "us-east-1"),
        "endpoint_url": os.environ.get("PK_S3_ENDPOINT_URL") or None,
    }


class PublicKnowledgeS3Store(S3JsonStore):
    def __init__(self, bucket: str = "", prefix: str = DEFAULT_PREFIX,
                 region: str = "us-east-1", endpoint_url: Optional[str] = None) -> None:
        super().__init__(bucket, prefix, region, endpoint_url)

    def get_doc(self, doc_id: str) -> Optional[PublicKnowledgeDoc]:
        data = self._get_json(f"docs/{doc_id}.json")
        return None if data is None else PublicKnowledgeDoc(**data)


class IndexStore(S3JsonStore):
    KEY = "index/latest.jsonl"

    def __init__(self, bucket: str = "", prefix: str = DEFAULT_PREFIX,
                 region: str = "us-east-1", endpoint_url: Optional[str] = None) -> None:
        super().__init__(bucket, prefix, region, endpoint_url)

    def load_index(self) -> List[PublicKnowledgeIndexEntry]:
        """Load all index entries; a missing or unreadable index is treated as empty."""
        try:
            body = self._get(self.KEY)
        except Exception as e:
            logger.warning("Failed to load index: %s (starting fresh)", e)
            return []
        entries: List[PublicKnowledgeIndexEntry] = []
        for line_num, line in enumerate((body or "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                entries.append(PublicKnowledgeIndexEntry(**json.loads(line)))
            except Exception as e:
                logger.warning("Skipping invalid index line %d: %s", line_num, e)
        return entries


def search(
    query: SearchQuery,
    index: List[PublicKnowledgeIndexEntry],
) -> List[PublicKnowledgeIndexEntry]:
    results = list(index)
    if query.tags:
        query_tags_lower = {t.lower() for t in query.tags}
        results = [e for e in results if any(t.lower() in query_tags_lower for t in e.tags)]
    if query.source_type:
        results = [e for e in results if e.source_type == query.source_type]
    if query.fresh_only:
        now = datetime.now(timezone.utc)
        results = [e for e in results if _is_fresh(e, now)]
    if query.keyword:
        kw = query.keyword.lower()
        results = [e for e in results if kw in e.title.lower()]
    results.sort(key=lambda e: e.published_at, reverse=True)
    return results[: query.limit]


def create_public_knowledge_router():
    from fastapi import APIRouter, HTTPException, Query

    router = APIRouter(prefix="/api/public-knowledge", tags=["public-knowledge"])
    settings = _s3_settings_from_env()
    store = PublicKnowledgeS3Store(**settings)
    index_store = IndexStore(**settings)

    @router.get("/search")
    def search_public_knowledge(
        tags: Optional[str] = Query(default=None, description="Comma-separated tags"),
        source_type: Optional[str] = Query(default=None, description="Filter by source type"),
        fresh: bool = Query(default=False, description="Only fresh documents"),
        q: Optional[str] = Query(default=None, description="Keyword search in title"),
        limit: int = Query(default=20, ge=1, le=200, description="Max results"),
    ):
        query = SearchQuery(
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
            source_type=source_type,
            fresh_only=fresh,
            keyword=q,
            limit=limit,
        )
        results = search(query, index_store.load_index())
        return {
            "results": [r.model_dump(mode="json") for r in results],
            "total": len(results),
            "query": query.model_dump(mode="json"),
        }

    @router.get("/{doc_id}")
    def get_public_knowledge_doc(doc_id: str):
        doc = store.get_doc(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")
        return doc.model_dump(mode="json")

    return router


def create_public_knowledge_tool_defs(
    bucket: str = "",
    prefix: str = DEFAULT_PREFIX,
    region: str = "us-east-1",
    endpoint_url: str = "",
) -> List:
    """Create the search/read ToolDefinitions. Arguments default to the PK_S3_* environment."""
    from ..agents.tools import ToolDefinition

    settings = _s3_settings_from_env()
    if bucket:
        settings = {"bucket": bucket, "prefix": prefix, "region": region, "endpoint_url": endpoint_url or None}
    _store = PublicKnowledgeS3Store(**settings)
    _index_store = IndexStore(**settings)

    @offload
    def search_public_knowledge(args: dict) -> str:
        tags_raw = args.get("tags", "")
        query = SearchQuery(
            tags=[t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None,
            source_type=args.get("source_type") or None,
            fresh_only=bool(args.get("fresh_only", False)),
            keyword=args.get("keyword") or None,
            limit=int(args.get("limit", 20)),
        )
        results = search(query, _index_store.load_index())
        return json.dumps([r.model_dump(mode="json") for r in results], ensure_ascii=False, indent=2)

    @offload
    def read_public_knowledge(args: dict) -> str:
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
