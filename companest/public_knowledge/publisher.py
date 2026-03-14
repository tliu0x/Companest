"""
PublicKnowledgePublisher - Coordinated publish/unpublish of documents.

Ensures that the S3 document store and the JSONL index stay in sync.
"""

import logging

from .index_store import IndexStore
from .models import PublicKnowledgeDoc, PublicKnowledgeIndexEntry
from .s3_store import PublicKnowledgeS3Store

logger = logging.getLogger(__name__)


class PublicKnowledgePublisher:
    """
    Coordinates writes to both the document store and the index.

    Usage:
        publisher = PublicKnowledgePublisher(s3_store, index_store)
        publisher.publish(doc)
        publisher.unpublish(doc_id)
    """

    def __init__(
        self,
        s3_store: PublicKnowledgeS3Store,
        index_store: IndexStore,
    ) -> None:
        self._s3_store = s3_store
        self._index_store = index_store

    def publish(self, doc: PublicKnowledgeDoc) -> str:
        """
        Publish a document: write to S3 and update the index.

        Returns:
            The S3 object key for the uploaded document.
        """
        # Write full document to S3
        key = self._s3_store.put_doc(doc)

        # Update index (add or replace entry)
        entry = PublicKnowledgeIndexEntry.from_doc(doc)
        self._index_store.add_entry(entry)

        logger.info("Published doc %s (%s)", doc.doc_id, doc.title[:80])
        return key

    def unpublish(self, doc_id: str) -> None:
        """
        Unpublish a document: remove from S3 and the index.
        """
        # Remove from S3
        self._s3_store.delete_doc(doc_id)

        # Remove from index
        self._index_store.remove_entry(doc_id)

        logger.info("Unpublished doc %s", doc_id)
