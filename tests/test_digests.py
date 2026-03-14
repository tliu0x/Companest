"""Tests for the digests subsystem."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from companest.digests.models import (
    DIGEST_TYPE_MAP,
    ApprovalQueueDigest,
    DigestEnvelope,
    MarketSnapshotDigest,
    PnLDigest,
    PositionsDigest,
    RiskDigest,
    SettlementDigest,
)
from companest.digests.ingest import DigestIngestor, IngestResult
from companest.digests.s3_store import DigestS3Store


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

class TestDigestModels:
    def test_market_snapshot_valid(self):
        d = MarketSnapshotDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-1",
            markets=[{
                "ticker": "TICK-1",
                "title": "Will X happen?",
                "yes_price": 65,
                "status": "open",
                "last_updated": NOW.isoformat(),
            }],
        )
        assert d.digest_type == "market_snapshot"
        assert len(d.markets) == 1

    def test_positions_valid(self):
        d = PositionsDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-2",
            positions=[{
                "ticker": "T1", "side": "yes", "quantity": 10, "avg_price": 50,
            }],
        )
        assert d.digest_type == "positions"

    def test_pnl_valid(self):
        d = PnLDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-3",
            realized_pnl=1000,
            unrealized_pnl=-200,
            total_pnl=800,
        )
        assert d.total_pnl == 800

    def test_risk_valid(self):
        d = RiskDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-4",
            total_exposure=5000,
            position_count=3,
            risk_level="medium",
        )
        assert d.risk_level == "medium"

    def test_settlement_valid(self):
        d = SettlementDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-5",
            settlements=[{
                "ticker": "T1", "outcome": "yes", "pnl": 500,
                "settled_at": NOW.isoformat(),
            }],
        )
        assert len(d.settlements) == 1

    def test_approval_queue_valid(self):
        d = ApprovalQueueDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="key-6",
            pending_approvals=[{
                "order_id": "ord-1", "ticker": "T1", "side": "yes",
                "quantity": 5, "price": 60, "reason": "over limit",
                "created_at": NOW.isoformat(),
            }],
        )
        assert len(d.pending_approvals) == 1

    def test_invalid_risk_level_rejected(self):
        with pytest.raises(Exception):
            RiskDigest(
                company_id="acme",
                snapshot_timestamp=NOW,
                idempotency_key="key-x",
                total_exposure=100,
                position_count=1,
                risk_level="unknown",
            )

    def test_extra_fields_rejected(self):
        with pytest.raises(Exception):
            MarketSnapshotDigest(
                company_id="acme",
                snapshot_timestamp=NOW,
                idempotency_key="key-x",
                markets=[],
                extra_field="nope",
            )

    def test_digest_type_map_complete(self):
        expected = {
            "market_snapshot", "positions", "pnl",
            "risk", "settlement", "approval_queue",
        }
        assert set(DIGEST_TYPE_MAP.keys()) == expected


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIngestIdempotency:
    def _make_ingestor(self):
        mock_store = MagicMock(spec=DigestS3Store)
        return DigestIngestor(mock_store), mock_store

    def test_first_ingest_accepted(self):
        ingestor, store = self._make_ingestor()
        digest = MarketSnapshotDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="k1",
            markets=[],
        )
        result = ingestor.ingest(digest)
        assert result.accepted is True
        assert result.reason == "accepted"
        store.put_digest.assert_called_once()

    def test_duplicate_returns_duplicate(self):
        ingestor, store = self._make_ingestor()
        digest = MarketSnapshotDigest(
            company_id="acme",
            snapshot_timestamp=NOW,
            idempotency_key="k1",
            markets=[],
        )
        ingestor.ingest(digest)
        result = ingestor.ingest(digest)
        assert result.accepted is True
        assert result.reason == "duplicate"
        # S3 should only be called once
        assert store.put_digest.call_count == 1

    def test_different_keys_both_accepted(self):
        ingestor, store = self._make_ingestor()
        d1 = MarketSnapshotDigest(
            company_id="acme", snapshot_timestamp=NOW,
            idempotency_key="k1", markets=[],
        )
        d2 = MarketSnapshotDigest(
            company_id="acme", snapshot_timestamp=NOW,
            idempotency_key="k2", markets=[],
        )
        r1 = ingestor.ingest(d1)
        r2 = ingestor.ingest(d2)
        assert r1.reason == "accepted"
        assert r2.reason == "accepted"
        assert store.put_digest.call_count == 2


# ---------------------------------------------------------------------------
# Company ID validation
# ---------------------------------------------------------------------------

class TestCompanyIdValidation:
    def test_valid_company_id(self):
        ingestor, _ = TestIngestIdempotency._make_ingestor(None)
        digest = MarketSnapshotDigest(
            company_id="acme-corp",
            snapshot_timestamp=NOW,
            idempotency_key="k1",
            markets=[],
        )
        result = ingestor.ingest(digest)
        assert result.accepted is True

    def test_invalid_company_id_rejected(self):
        ingestor, _ = TestIngestIdempotency._make_ingestor(None)
        digest = MarketSnapshotDigest(
            company_id="../../../etc/passwd",
            snapshot_timestamp=NOW,
            idempotency_key="k1",
            markets=[],
        )
        result = ingestor.ingest(digest)
        assert result.accepted is False
        assert result.reason == "invalid_company_id"

    def _make_ingestor(self):
        mock_store = MagicMock(spec=DigestS3Store)
        return DigestIngestor(mock_store), mock_store


# ---------------------------------------------------------------------------
# S3 store
# ---------------------------------------------------------------------------

class TestDigestS3Store:
    def test_put_digest_uploads_and_updates_latest(self):
        mock_s3 = MagicMock()
        store = DigestS3Store(bucket="test-bucket")
        store._s3 = mock_s3

        store.put_digest("acme", "positions", {"data": "test"}, "key-1")

        assert mock_s3.put_object.call_count == 2  # versioned + latest
        keys = [c[1]["Key"] for c in mock_s3.put_object.call_args_list]
        assert any("key-1.json" in k for k in keys)
        assert any("latest.json" in k for k in keys)
