"""
Companest Digest Ingestor

Validates, deduplicates, and persists incoming digest payloads.
v1: in-memory idempotency tracking (single-instance).
"""

import logging
from dataclasses import dataclass
from typing import Set

from companest.company import _SAFE_ID_RE

from .models import DigestEnvelope
from .s3_store import DigestS3Store

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Result of a digest ingestion attempt."""

    accepted: bool
    reason: str
    digest_type: str
    company_id: str


class DigestIngestor:
    """
    Validates and ingests digest payloads into S3 storage.

    Idempotency is enforced in-memory (v1 single-instance): a digest with the
    same (company_id, digest_type, idempotency_key) tuple is accepted silently
    on subsequent submissions without re-uploading.
    """

    def __init__(self, s3_store: DigestS3Store) -> None:
        self._s3_store = s3_store
        self._seen_keys: Set[str] = set()

    def _make_dedup_key(self, digest: DigestEnvelope) -> str:
        """Build a deduplication key from envelope fields."""
        return f"{digest.company_id}:{digest.digest_type}:{digest.idempotency_key}"

    def ingest(self, digest: DigestEnvelope) -> IngestResult:
        """
        Ingest a digest payload.

        Steps:
        1. Validate company_id against _SAFE_ID_RE
        2. Check idempotency (in-memory dedup)
        3. Upload to S3
        """
        company_id = digest.company_id
        digest_type = digest.digest_type

        # 1. Validate company_id
        if not _SAFE_ID_RE.match(company_id):
            logger.warning(
                "Rejected digest: invalid company_id %r", company_id
            )
            return IngestResult(
                accepted=False,
                reason="invalid_company_id",
                digest_type=digest_type,
                company_id=company_id,
            )

        # 2. Idempotency check
        dedup_key = self._make_dedup_key(digest)
        if dedup_key in self._seen_keys:
            logger.debug(
                "Duplicate digest ignored: %s/%s key=%s",
                company_id, digest_type, digest.idempotency_key,
            )
            return IngestResult(
                accepted=True,
                reason="duplicate",
                digest_type=digest_type,
                company_id=company_id,
            )

        # 3. Upload to S3
        try:
            digest_data = digest.model_dump(mode="json")
            self._s3_store.put_digest(
                company_id=company_id,
                digest_type=digest_type,
                digest_data=digest_data,
                idempotency_key=digest.idempotency_key,
            )
        except Exception as e:
            logger.error(
                "Failed to store digest %s/%s: %s",
                company_id, digest_type, e,
            )
            return IngestResult(
                accepted=False,
                reason=f"storage_error: {e}",
                digest_type=digest_type,
                company_id=company_id,
            )

        # Mark as seen after successful upload
        self._seen_keys.add(dedup_key)

        logger.info(
            "Ingested digest %s/%s key=%s",
            company_id, digest_type, digest.idempotency_key,
        )
        return IngestResult(
            accepted=True,
            reason="accepted",
            digest_type=digest_type,
            company_id=company_id,
        )
