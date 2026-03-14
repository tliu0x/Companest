"""
Companest Digests  Company-Private Digest Ingestion Layer

Accepts structured digest payloads (market snapshots, positions, PnL, risk,
settlements, approval queues) from external trader systems and stores them
per-company in S3.

Digests are idempotent: duplicate submissions (same company_id + digest_type
+ idempotency_key) are accepted silently without re-uploading.
"""

from .models import (
    DigestEnvelope,
    MarketSnapshotDigest,
    PositionsDigest,
    PnLDigest,
    RiskDigest,
    SettlementDigest,
    ApprovalQueueDigest,
    DIGEST_TYPE_MAP,
)
from .ingest import DigestIngestor, IngestResult
from .s3_store import DigestS3Store

# Note: router is NOT imported at package level to avoid forcing
# fastapi as a hard dependency. Import it directly when needed:
#   from companest.digests.router import router as digest_router

__all__ = [
    "DigestEnvelope",
    "MarketSnapshotDigest",
    "PositionsDigest",
    "PnLDigest",
    "RiskDigest",
    "SettlementDigest",
    "ApprovalQueueDigest",
    "DIGEST_TYPE_MAP",
    "DigestIngestor",
    "IngestResult",
    "DigestS3Store",
]
