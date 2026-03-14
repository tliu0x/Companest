"""
FastAPI router for the Public Knowledge API.

Prefix: /api/public-knowledge

Endpoints:
- GET /search  - search the index by tags, source_type, freshness, keyword
- GET /{doc_id} - retrieve the full document

This router is standalone and will be included in server.py in Phase 2.
"""

import logging
import os
from typing import Optional

from .models import SearchQuery
from .search import search as search_index

logger = logging.getLogger(__name__)


def create_public_knowledge_router():
    """
    Create and return a FastAPI APIRouter for public knowledge endpoints.

    Guarded by the ENABLE_PUBLIC_KNOWLEDGE_V1 feature flag.
    """
    try:
        from fastapi import APIRouter, HTTPException, Query
    except ImportError:
        raise ImportError(
            "FastAPI required. Install with: pip install fastapi"
        )

    from .index_store import IndexStore
    from .s3_store import PublicKnowledgeS3Store

    router = APIRouter(prefix="/api/public-knowledge", tags=["public-knowledge"])

    # Lazy-init shared instances (configured from env)
    _store: Optional[PublicKnowledgeS3Store] = None
    _index: Optional[IndexStore] = None

    def _get_store() -> PublicKnowledgeS3Store:
        nonlocal _store
        if _store is None:
            _store = PublicKnowledgeS3Store(
                bucket=os.environ.get("PK_S3_BUCKET", ""),
                prefix=os.environ.get("PK_S3_PREFIX", "companest-public-knowledge/"),
                region=os.environ.get("PK_S3_REGION", "us-east-1"),
                endpoint_url=os.environ.get("PK_S3_ENDPOINT_URL") or None,
            )
        return _store

    def _get_index_store() -> IndexStore:
        nonlocal _index
        if _index is None:
            _index = IndexStore(
                bucket=os.environ.get("PK_S3_BUCKET", ""),
                prefix=os.environ.get("PK_S3_PREFIX", "companest-public-knowledge/"),
                region=os.environ.get("PK_S3_REGION", "us-east-1"),
                endpoint_url=os.environ.get("PK_S3_ENDPOINT_URL") or None,
            )
        return _index

    def _check_feature_flag() -> None:
        if not os.environ.get("ENABLE_PUBLIC_KNOWLEDGE_V1", "").lower() in ("1", "true"):
            raise HTTPException(
                status_code=404,
                detail="Public Knowledge API is not enabled (set ENABLE_PUBLIC_KNOWLEDGE_V1=1)",
            )

    @router.get("/search")
    async def search_public_knowledge(
        tags: Optional[str] = Query(default=None, description="Comma-separated tags"),
        source_type: Optional[str] = Query(default=None, description="Filter by source type"),
        fresh: bool = Query(default=False, description="Only fresh documents"),
        q: Optional[str] = Query(default=None, description="Keyword search in title"),
        limit: int = Query(default=20, ge=1, le=200, description="Max results"),
    ):
        """Search the public knowledge index."""
        _check_feature_flag()

        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

        query = SearchQuery(
            tags=parsed_tags,
            source_type=source_type,
            fresh_only=fresh,
            keyword=q,
            limit=limit,
        )

        index_store = _get_index_store()
        entries = index_store.load_index()
        results = search_index(query, entries)

        return {
            "results": [r.model_dump(mode="json") for r in results],
            "total": len(results),
            "query": query.model_dump(mode="json"),
        }

    @router.get("/{doc_id}")
    async def get_public_knowledge_doc(doc_id: str):
        """Retrieve a full public knowledge document by ID."""
        _check_feature_flag()

        store = _get_store()
        doc = store.get_doc(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")
        return doc.model_dump(mode="json")

    return router
