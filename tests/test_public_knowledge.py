"""Tests for the public_knowledge subsystem."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from companest.knowledge.public import (
    INDEX_CAP,
    IndexStore,
    PublicKnowledgeDoc,
    PublicKnowledgeIndexEntry,
    PublicKnowledgePublisher,
    PublicKnowledgeS3Store,
    SearchQuery,
    search,
)


NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=7)
PAST = NOW - timedelta(days=1)


def _make_doc(doc_id=None, source_type="news", title="Test Doc",
              tags=None, fresh_until=None, visibility="public", confidence=0.8):
    return PublicKnowledgeDoc(
        doc_id=doc_id or str(uuid.uuid4()),
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


def _make_index_entry(doc_id=None, **kwargs):
    doc = _make_doc(doc_id=doc_id, **kwargs)
    return PublicKnowledgeIndexEntry.from_doc(doc)


def test_non_uuid_doc_id_rejected():
    with pytest.raises(Exception):
        _make_doc(doc_id="not-a-uuid")


class TestSearch:
    D1 = "00000000-0000-0000-0000-000000000001"
    D2 = "00000000-0000-0000-0000-000000000002"
    D3 = "00000000-0000-0000-0000-000000000003"

    def _entries(self):
        return [
            _make_index_entry(self.D1, title="Fed Rate Decision",
                              tags=["topic:macro", "entity:fed"],
                              source_type="news"),
            _make_index_entry(self.D2, title="Kalshi Market Overview",
                              tags=["market:kalshi", "topic:prediction"],
                              source_type="prediction_market"),
            _make_index_entry(self.D3, title="Old Expired Doc",
                              tags=["topic:test"],
                              fresh_until=PAST,
                              source_type="research"),
        ]

    @pytest.mark.parametrize("query, expected", [
        ({}, {D1, D2, D3}),
        ({"tags": ["topic:macro"]}, {D1}),
        ({"source_type": "prediction_market"}, {D2}),
        ({"fresh_only": True}, {D1, D2}),
        ({"keyword": "kalshi"}, {D2}),
        ({"keyword": "FED"}, {D1}),
    ], ids=["no_filters", "tags", "source_type", "fresh_only", "keyword", "keyword_case_insensitive"])
    def test_filters(self, query, expected):
        results = search(SearchQuery(**query), self._entries())
        assert len(results) == len(expected)
        assert {r.doc_id for r in results} == expected

    def test_limit(self):
        results = search(SearchQuery(limit=1), self._entries())
        assert len(results) == 1

    def test_sort_by_published_at_desc(self):
        entries = [
            e.model_copy(update={"published_at": NOW - timedelta(hours=age)})
            for e, age in zip(self._entries(), (2, 0, 1))
        ]
        assert [r.doc_id for r in search(SearchQuery(), entries)] == [self.D2, self.D3, self.D1]


class TestIndexEviction:
    def _saved_lines(self, entries):
        mock_s3 = MagicMock()
        store = IndexStore(bucket="test-bucket")
        store._s3 = mock_s3
        store.save_index(entries)
        body = mock_s3.put_object.call_args[1]["Body"].decode("utf-8")
        return [line for line in body.strip().split("\n") if line]

    def test_expired_entries_evicted_on_save(self):
        fresh_id = str(uuid.uuid4())
        lines = self._saved_lines([
            _make_index_entry(fresh_id, fresh_until=FUTURE),
            _make_index_entry(str(uuid.uuid4()), fresh_until=PAST),
        ])
        assert len(lines) == 1
        assert fresh_id in lines[0]

    def test_cap_enforced(self):
        lines = self._saved_lines([
            _make_index_entry(str(uuid.UUID(int=i)), fresh_until=FUTURE)
            for i in range(INDEX_CAP + 100)
        ])
        assert len(lines) == INDEX_CAP


class TestPublisher:
    def _publisher(self):
        s3_store = MagicMock(spec=PublicKnowledgeS3Store)
        index_store = MagicMock(spec=IndexStore)
        return PublicKnowledgePublisher(s3_store, index_store), s3_store, index_store

    def test_publish_writes_doc_and_index(self):
        publisher, s3_store, index_store = self._publisher()
        doc = _make_doc()
        publisher.publish(doc)

        s3_store.put_doc.assert_called_once_with(doc)
        index_store.add_entry.assert_called_once()

    def test_unpublish_removes_doc_and_index(self):
        publisher, s3_store, index_store = self._publisher()
        test_id = str(uuid.uuid4())
        publisher.unpublish(test_id)

        s3_store.delete_doc.assert_called_once_with(test_id)
        index_store.remove_entry.assert_called_once_with(test_id)
