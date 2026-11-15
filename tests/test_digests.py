"""Tests for the digests subsystem."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from companest.knowledge.digests import (
    DigestIngestor,
    DigestS3Store,
    MarketSnapshotDigest,
)


NOW = datetime.now(timezone.utc)


def _snapshot(company_id="acme", key="k1"):
    return MarketSnapshotDigest(
        company_id=company_id, snapshot_timestamp=NOW, idempotency_key=key, markets=[],
    )


def _make_ingestor():
    mock_store = MagicMock(spec=DigestS3Store)
    return DigestIngestor(mock_store), mock_store


class TestDigestModels:
    def test_extra_fields_rejected(self):
        with pytest.raises(Exception):
            MarketSnapshotDigest(
                company_id="acme",
                snapshot_timestamp=NOW,
                idempotency_key="key-x",
                markets=[],
                extra_field="nope",
            )


class TestIngest:
    def test_duplicate_is_stored_once(self):
        ingestor, store = _make_ingestor()
        first = ingestor.ingest(_snapshot())
        assert first.accepted is True
        assert first.reason == "accepted"
        store.put_digest.assert_called_once()

        result = ingestor.ingest(_snapshot())
        assert result.accepted is True
        assert result.reason == "duplicate"
        assert store.put_digest.call_count == 1

    def test_different_keys_both_accepted(self):
        ingestor, store = _make_ingestor()
        r1 = ingestor.ingest(_snapshot(key="k1"))
        r2 = ingestor.ingest(_snapshot(key="k2"))
        assert r1.reason == "accepted"
        assert r2.reason == "accepted"
        assert store.put_digest.call_count == 2

    def test_invalid_company_id_rejected(self):
        ingestor, store = _make_ingestor()
        result = ingestor.ingest(_snapshot(company_id="../../../etc/passwd"))
        assert result.accepted is False
        assert result.reason == "invalid_company_id"
        store.put_digest.assert_not_called()


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
