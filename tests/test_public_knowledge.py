"""Tests for the public_knowledge subsystem."""

import json
import uuid
from io import BytesIO
from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from companest.knowledge.public import (
    IndexStore,
    PublicKnowledgeDoc,
    PublicKnowledgeIndexEntry,
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

def test_reads_existing_documents_and_index():
    doc = _make_doc()
    entry = PublicKnowledgeIndexEntry.from_doc(doc)
    s3 = MagicMock()
    s3.get_object.side_effect = [
        {"Body": BytesIO(doc.model_dump_json().encode())},
        {"Body": BytesIO((entry.model_dump_json() + "\ninvalid json\n").encode())},
    ]
    docs = PublicKnowledgeS3Store(bucket="test-bucket")
    index = IndexStore(bucket="test-bucket")
    docs._s3 = index._s3 = s3

    assert docs.get_doc(doc.doc_id) == doc
    assert index.load_index() == [entry]
    assert search(SearchQuery(keyword="Test"), index=[entry]) == [entry]
    s3.put_object.assert_not_called()
    s3.delete_object.assert_not_called()


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
