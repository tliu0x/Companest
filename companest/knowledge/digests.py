"""
Company-private digest ingestion.

External trader systems push structured digests (market snapshots, positions,
PnL, risk, settlements, approval queues). Each digest is validated, deduplicated
by (company_id, digest_type, idempotency_key) and stored per company in S3:

    {prefix}{company_id}/{digest_type}/{idempotency_key}.json
    {prefix}{company_id}/{digest_type}/latest.json

The JSON schemas in contracts/digests/ mirror the models below.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Set, Type

from pydantic import BaseModel, Field

from ..company import _SAFE_ID_RE
from .s3 import S3JsonStore

logger = logging.getLogger(__name__)


# -- Envelope (shared fields) --


class DigestEnvelope(BaseModel):
    """Common envelope fields shared by all digest types."""

    company_id: str = Field(..., description="Company that owns this digest")
    digest_type: str = Field(..., description="Type discriminator")
    schema_version: str = Field(..., description="Schema version string")
    snapshot_timestamp: datetime = Field(
        ..., description="When this snapshot was taken (ISO8601)"
    )
    idempotency_key: str = Field(
        ..., description="Unique key for deduplication"
    )

    model_config = {"extra": "forbid"}


# -- Nested item models --


class MarketItem(BaseModel):
    """A single market entry in a market snapshot."""

    ticker: str
    title: str
    yes_price: float = Field(..., ge=0, le=100)
    volume: Optional[int] = Field(default=None, ge=0)
    status: Literal["open", "closed", "settled"]
    last_updated: datetime

    model_config = {"extra": "forbid"}


class PositionItem(BaseModel):
    """A single position entry."""

    ticker: str
    market_title: Optional[str] = None
    side: Literal["yes", "no"]
    quantity: int = Field(..., ge=0)
    avg_price: float = Field(..., ge=0, le=100)
    current_price: Optional[float] = Field(default=None, ge=0, le=100)
    unrealized_pnl: Optional[float] = None

    model_config = {"extra": "forbid"}


class SettlementItem(BaseModel):
    """A single settlement event."""

    ticker: str
    market_title: Optional[str] = None
    outcome: Literal["yes", "no"]
    quantity: Optional[int] = Field(default=None, ge=0)
    pnl: float = Field(..., description="Settlement P&L in cents")
    settled_at: datetime

    model_config = {"extra": "forbid"}


class ApprovalItem(BaseModel):
    """A single pending approval entry."""

    order_id: str
    ticker: str
    market_title: Optional[str] = None
    side: Literal["yes", "no"]
    quantity: int = Field(..., ge=1)
    price: float = Field(..., ge=1, le=99)
    reason: str = Field(..., description="Why this order needs approval")
    created_at: datetime

    model_config = {"extra": "forbid"}


# -- Digest models --


class MarketSnapshotDigest(DigestEnvelope):
    """Snapshot of markets a trader is actively watching."""

    digest_type: Literal["market_snapshot"] = "market_snapshot"
    schema_version: Literal["1.0.0"] = "1.0.0"
    markets: List[MarketItem]


class PositionsDigest(DigestEnvelope):
    """Current positions held by a trader."""

    digest_type: Literal["positions"] = "positions"
    schema_version: Literal["1.0.0"] = "1.0.0"
    positions: List[PositionItem]


class PnLDigest(DigestEnvelope):
    """Profit and loss summary for a trader."""

    digest_type: Literal["pnl"] = "pnl"
    schema_version: Literal["1.0.0"] = "1.0.0"
    daily_pnl: Optional[float] = Field(
        default=None, description="P&L for current day in cents"
    )
    weekly_pnl: Optional[float] = Field(
        default=None, description="P&L for current week in cents"
    )
    realized_pnl: float = Field(
        ..., description="Total realized P&L in cents"
    )
    unrealized_pnl: float = Field(
        ..., description="Total unrealized P&L in cents"
    )
    total_pnl: float = Field(
        ..., description="realized + unrealized in cents"
    )


class RiskDigest(DigestEnvelope):
    """Risk metrics for a trader."""

    digest_type: Literal["risk"] = "risk"
    schema_version: Literal["1.0.0"] = "1.0.0"
    total_exposure: float = Field(
        ..., ge=0, description="Total capital at risk in cents"
    )
    position_count: int = Field(
        ..., ge=0, description="Number of open positions"
    )
    max_single_position: Optional[float] = Field(
        default=None, ge=0, description="Largest single position exposure in cents"
    )
    daily_loss: Optional[float] = Field(
        default=None, description="Current day loss in cents (negative = loss)"
    )
    daily_loss_limit: Optional[float] = Field(
        default=None, ge=0, description="Configured daily loss limit in cents"
    )
    position_limit: Optional[int] = Field(
        default=None, ge=0, description="Configured max number of positions"
    )
    risk_level: Literal["low", "medium", "high", "critical"]


class SettlementDigest(DigestEnvelope):
    """Recent settlement events for a trader."""

    digest_type: Literal["settlement"] = "settlement"
    schema_version: Literal["1.0.0"] = "1.0.0"
    settlements: List[SettlementItem]


class ApprovalQueueDigest(DigestEnvelope):
    """Pending trade approvals awaiting human review."""

    digest_type: Literal["approval_queue"] = "approval_queue"
    schema_version: Literal["1.0.0"] = "1.0.0"
    pending_approvals: List[ApprovalItem]


# -- Type map --

DIGEST_TYPE_MAP: Dict[str, Type[DigestEnvelope]] = {
    "market_snapshot": MarketSnapshotDigest,
    "positions": PositionsDigest,
    "pnl": PnLDigest,
    "risk": RiskDigest,
    "settlement": SettlementDigest,
    "approval_queue": ApprovalQueueDigest,
}


# -- Storage --


class DigestS3Store(S3JsonStore):
    """S3-backed storage for company-private digests."""

    def __init__(self, bucket: str = "", prefix: str = "companest-digests/",
                 region: str = "us-east-1", endpoint_url: Optional[str] = None) -> None:
        super().__init__(bucket, prefix, region, endpoint_url)

    def put_digest(self, company_id: str, digest_type: str,
                   digest_data: Dict[str, Any], idempotency_key: str) -> str:
        """Store a digest and update the latest pointer. Returns the S3 key."""
        body = json.dumps(digest_data, ensure_ascii=False, default=str)
        base = f"{company_id}/{digest_type}"
        key = self._put(f"{base}/{idempotency_key}.json", body)
        self._put(f"{base}/latest.json", body)
        return key

    def get_latest_digest(self, company_id: str, digest_type: str) -> Optional[Dict[str, Any]]:
        """Return the latest digest for a company and type, or None."""
        return self._get_json(f"{company_id}/{digest_type}/latest.json")


# -- Ingestion --


@dataclass
class IngestResult:
    """Result of a digest ingestion attempt."""

    accepted: bool
    reason: str
    digest_type: str
    company_id: str


class DigestIngestor:
    """
    Validates, deduplicates and stores digests.

    Idempotency is tracked in memory (single instance): a repeated
    (company_id, digest_type, idempotency_key) is accepted without re-uploading.
    """

    def __init__(self, s3_store: DigestS3Store) -> None:
        self._s3_store = s3_store
        self._seen_keys: Set[str] = set()

    def ingest(self, digest: DigestEnvelope) -> IngestResult:
        def result(accepted: bool, reason: str) -> IngestResult:
            return IngestResult(accepted, reason, digest.digest_type, digest.company_id)

        if not _SAFE_ID_RE.match(digest.company_id):
            logger.warning("Rejected digest: invalid company_id %r", digest.company_id)
            return result(False, "invalid_company_id")

        dedup_key = f"{digest.company_id}:{digest.digest_type}:{digest.idempotency_key}"
        if dedup_key in self._seen_keys:
            return result(True, "duplicate")

        try:
            self._s3_store.put_digest(
                company_id=digest.company_id,
                digest_type=digest.digest_type,
                digest_data=digest.model_dump(mode="json"),
                idempotency_key=digest.idempotency_key,
            )
        except Exception as e:
            logger.error("Failed to store digest %s: %s", dedup_key, e)
            return result(False, f"storage_error: {e}")

        self._seen_keys.add(dedup_key)
        return result(True, "accepted")


# -- HTTP API --


def create_digest_router(s3_store: DigestS3Store):
    """Build the FastAPI router for digest ingestion and retrieval."""
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/api/companies")
    ingestor = DigestIngestor(s3_store)

    def check(company_id: str, digest_type: str) -> Type[DigestEnvelope]:
        if not _SAFE_ID_RE.match(company_id):
            raise HTTPException(status_code=400, detail="Invalid company_id")
        model_cls = DIGEST_TYPE_MAP.get(digest_type)
        if model_cls is None:
            raise HTTPException(status_code=400, detail=f"Unknown digest_type: {digest_type}")
        return model_cls

    @router.post("/{company_id}/digests/{digest_type}")
    async def ingest_digest(company_id: str, digest_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        model_cls = check(company_id, digest_type)
        # Path parameters are authoritative for the envelope fields.
        payload["company_id"] = company_id
        payload["digest_type"] = digest_type
        try:
            digest = model_cls.model_validate(payload)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))

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
    async def get_latest_digest(company_id: str, digest_type: str) -> Dict[str, Any]:
        check(company_id, digest_type)
        data = s3_store.get_latest_digest(company_id, digest_type)
        if data is None:
            raise HTTPException(status_code=404, detail=f"No digest found for {company_id}/{digest_type}")
        return data

    return router
