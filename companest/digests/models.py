"""
Companest Digest Pydantic Models

Typed models for each digest schema defined in contracts/digests/.
All digests share a common DigestEnvelope base with envelope fields.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional, Type

from pydantic import BaseModel, Field


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
