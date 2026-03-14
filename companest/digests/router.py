"""
Companest Digest FastAPI Router

Standalone router for digest ingestion and retrieval.
Prefix: /api/companies
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from companest.company import _SAFE_ID_RE

from .models import DIGEST_TYPE_MAP, DigestEnvelope
from .ingest import DigestIngestor
from .s3_store import DigestS3Store

logger = logging.getLogger(__name__)

# -- Module-level singletons (wired at import or startup) --

_s3_store: DigestS3Store | None = None
_ingestor: DigestIngestor | None = None


def configure(s3_store: DigestS3Store) -> None:
    """Wire the router to a concrete S3 store (call at app startup)."""
    global _s3_store, _ingestor
    _s3_store = s3_store
    _ingestor = DigestIngestor(s3_store)


def _get_ingestor() -> DigestIngestor:
    if _ingestor is None:
        raise HTTPException(
            status_code=503, detail="Digest ingestor not configured"
        )
    return _ingestor


def _get_store() -> DigestS3Store:
    if _s3_store is None:
        raise HTTPException(
            status_code=503, detail="Digest S3 store not configured"
        )
    return _s3_store


router = APIRouter(prefix="/api/companies")


@router.post("/{company_id}/digests/{digest_type}")
async def ingest_digest(
    company_id: str,
    digest_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Accept and ingest a company digest."""
    # Validate company_id
    if not _SAFE_ID_RE.match(company_id):
        raise HTTPException(status_code=400, detail="Invalid company_id")

    # Validate digest_type
    model_cls = DIGEST_TYPE_MAP.get(digest_type)
    if model_cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown digest_type: {digest_type}",
        )

    # Ensure envelope fields match path params
    payload["company_id"] = company_id
    payload["digest_type"] = digest_type

    # Parse and validate
    try:
        digest = model_cls.model_validate(payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Ingest
    ingestor = _get_ingestor()
    result = ingestor.ingest(digest)

    if not result.accepted:
        raise HTTPException(status_code=400, detail=result.reason)

    return {
        "accepted": result.accepted,
        "reason": result.reason,
        "digest_type": result.digest_type,
        "company_id": result.company_id,
    }


@router.get("/{company_id}/digests/{digest_type}/latest")
async def get_latest_digest(
    company_id: str,
    digest_type: str,
) -> Dict[str, Any]:
    """Return the latest digest for a company + type."""
    # Validate company_id
    if not _SAFE_ID_RE.match(company_id):
        raise HTTPException(status_code=400, detail="Invalid company_id")

    if digest_type not in DIGEST_TYPE_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown digest_type: {digest_type}",
        )

    store = _get_store()
    data = store.get_latest_digest(company_id, digest_type)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No digest found for {company_id}/{digest_type}",
        )

    return data
