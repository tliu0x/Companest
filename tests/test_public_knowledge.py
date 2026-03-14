"""Tests for the public_knowledge subsystem."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from companest.public_knowledge.models import (
    PublicKnowledgeDoc,
    PublicKnowledgeIndexEntry,
    SearchQuery,
)
from companest.public_knowledge.search import search
from companest.public_knowledge.index_store import IndexStore, INDEX_CAP
from companest.public_knowledge.publisher import PublicKnowledgePublisher
from companest.public_knowledge.s3_store import PublicKnowledgeS3Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=7)
PAST = NOW - timedelta(days=1)


def _make_doc(doc_id="doc-1", source_type="news", title="Test Doc",
              tags=None, fresh_until=None, visibility="public", confidence=0.8):
    return PublicKnowledgeDoc(
        doc_id=doc_id,
        source_type=source_type,
        title=title,
        summary="A test document summary.",
        tags=tags or ["topic:test"],
        source_url="https://example.com/doc",
        published_at=NOW,
        fresh_until=fresh_until or FUTURE,
        confidence=confidence,
        visibility=visibility,
    )


def _make_index_entry(doc_id="doc-1", **kwargs):
    doc = _make_doc(doc_id=doc_id, **kwargs)
    return PublicKnowledgeIndexEntry.from_doc(doc)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestPublicKnowledgeDoc:
    def test_valid_doc(self):
        doc = _make_doc()
        assert doc.doc_id == "doc-1"
        assert doc.source_type == "news"

    def test_invalid_source_type(self):
        with pytest.raises(Exception):
            _make_doc(source_type="invalid")

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            _make_doc(confidence=1.5)
        with pytest.raises(Exception):
            _make_doc(confidence=-0.1)

    def test_invalid_visibility(self):
        with pytest.raises(Exception):
            _make_doc(visibility="secret")

    def test_empty_title_rejected(self):
        with pytest.raises(Exception):
            _make_doc(title="")


class TestIndexEntry:
    def test_from_doc_drops_summary(self):
        doc = _make_doc()
        entry = PublicKnowledgeIndexEntry.from_doc(doc)
        assert not hasattr(entry, "summary") or "summary" not in entry.model_fields
        assert entry.doc_id == doc.doc_id
        assert entry.title == doc.title


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------

class TestSearch:
    def _entries(self):
        return [
            _make_index_entry("d1", title="Fed Rate Decision",
                              tags=["topic:macro", "entity:fed"],
                              source_type="news"),
            _make_index_entry("d2", title="Kalshi Market Overview",
                              tags=["market:kalshi", "topic:prediction"],
                              source_type="prediction_market"),
            _make_index_entry("d3", title="Old Expired Doc",
                              tags=["topic:test"],
                              fresh_until=PAST,
                              source_type="research"),
        ]

    def test_no_filters(self):
        results = search(SearchQuery(), self._entries())
        assert len(results) == 3

    def test_filter_by_tags(self):
        results = search(SearchQuery(tags=["topic:macro"]), self._entries())
        assert len(results) == 1
        assert results[0].doc_id == "d1"

    def test_filter_by_source_type(self):
        results = search(
            SearchQuery(source_type="prediction_market"), self._entries()
        )
        assert len(results) == 1
        assert results[0].doc_id == "d2"

    def test_filter_fresh_only(self):
        results = search(SearchQuery(fresh_only=True), self._entries())
        assert all(r.doc_id != "d3" for r in results)

    def test_filter_keyword(self):
        results = search(SearchQuery(keyword="kalshi"), self._entries())
        assert len(results) == 1
        assert results[0].doc_id == "d2"

    def test_keyword_case_insensitive(self):
        results = search(SearchQuery(keyword="FED"), self._entries())
        assert len(results) == 1

    def test_limit(self):
        results = search(SearchQuery(limit=1), self._entries())
        assert len(results) == 1

    def test_sort_by_published_at_desc(self):
        results = search(SearchQuery(), self._entries())
        for i in range(len(results) - 1):
            assert results[i].published_at >= results[i + 1].published_at


# ---------------------------------------------------------------------------
# Index eviction tests
# ---------------------------------------------------------------------------

class TestIndexEviction:
    def test_expired_entries_evicted_on_save(self):
        mock_s3 = MagicMock()
        store = IndexStore(bucket="test-bucket")
        store._s3 = mock_s3

        entries = [
            _make_index_entry("fresh", fresh_until=FUTURE),
            _make_index_entry("expired", fresh_until=PAST),
        ]
        store.save_index(entries)

        # Check what was written to S3
        call_args = mock_s3.put_object.call_args
        body = call_args[1]["Body"].decode("utf-8") if isinstance(call_args[1]["Body"], bytes) else call_args[1]["Body"]
        lines = [l for l in body.strip().split("\n") if l]
        assert len(lines) == 1
        assert "fresh" in lines[0]

    def test_cap_enforced(self):
        mock_s3 = MagicMock()
        store = IndexStore(bucket="test-bucket")
        store._s3 = mock_s3

        entries = [
            _make_index_entry(f"doc-{i}", fresh_until=FUTURE)
            for i in range(INDEX_CAP + 100)
        ]
        store.save_index(entries)

        call_args = mock_s3.put_object.call_args
        body = call_args[1]["Body"].decode("utf-8")
        lines = [l for l in body.strip().split("\n") if l]
        assert len(lines) == INDEX_CAP


# ---------------------------------------------------------------------------
# Publisher tests
# ---------------------------------------------------------------------------

class TestPublisher:
    def test_publish_writes_doc_and_index(self):
        mock_s3_store = MagicMock(spec=PublicKnowledgeS3Store)
        mock_index_store = MagicMock(spec=IndexStore)
        publisher = PublicKnowledgePublisher(mock_s3_store, mock_index_store)

        doc = _make_doc()
        publisher.publish(doc)

        mock_s3_store.put_doc.assert_called_once_with(doc)
        mock_index_store.add_entry.assert_called_once()

    def test_unpublish_removes_doc_and_index(self):
        mock_s3_store = MagicMock(spec=PublicKnowledgeS3Store)
        mock_index_store = MagicMock(spec=IndexStore)
        publisher = PublicKnowledgePublisher(mock_s3_store, mock_index_store)

        publisher.unpublish("doc-1")

        mock_s3_store.delete_doc.assert_called_once_with("doc-1")
        mock_index_store.remove_entry.assert_called_once_with("doc-1")
