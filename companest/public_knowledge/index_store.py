"""
JSONL index management for Public Knowledge.

Maintains an index/latest.jsonl file in S3 containing all index entries
(PublicKnowledgeIndexEntry) for efficient search without per-doc S3 reads.

Index cap: 10,000 entries. Expired docs (fresh_until < now) are evicted
on every write operation.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import PublicKnowledgeIndexEntry

logger = logging.getLogger(__name__)

INDEX_CAP = 10_000


class IndexStore:
    """
    Manages the index/latest.jsonl file in S3.

    The index is a JSONL file where each line is a serialized
    PublicKnowledgeIndexEntry (all doc fields except summary).
    """

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "companest-public-knowledge/",
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.region = region
        self.endpoint_url = endpoint_url
        self._s3 = None

    def _get_s3(self):
        """Lazy-init S3 client."""
        if self._s3 is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for IndexStore. "
                    "Install with: pip install companest-orchestrator[s3]"
                )
            kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _index_key(self) -> str:
        """S3 key for the index file."""
        return f"{self.prefix}index/latest.jsonl"

    def load_index(self) -> List[PublicKnowledgeIndexEntry]:
        """
        Load the full index from S3.

        Returns:
            List of index entries. Empty list if index does not exist.
        """
        key = self._index_key()
        s3 = self._get_s3()

        try:
            resp = s3.get_object(Bucket=self.bucket, Key=key)
            body = resp["Body"].read().decode("utf-8")
        except Exception as e:
            # Treat missing index as empty.
            # boto3 ClientError has e.response["Error"]["Code"] == "NoSuchKey".
            error_code = ""
            if hasattr(e, "response") and isinstance(e.response, dict):
                error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                logger.info("Index not found, starting fresh")
                return []
            logger.warning("Failed to load index: %s (starting fresh)", e)
            return []

        entries: List[PublicKnowledgeIndexEntry] = []
        for line_num, line in enumerate(body.strip().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries.append(PublicKnowledgeIndexEntry(**data))
            except Exception as e:
                logger.warning("Skipping invalid index line %d: %s", line_num, e)

        logger.info("Loaded index with %d entries", len(entries))
        return entries

    def save_index(self, entries: List[PublicKnowledgeIndexEntry]) -> None:
        """
        Atomically write the full index to S3.

        Evicts expired entries and enforces the cap before writing.
        """
        # Evict expired entries (normalize timezone for comparison)
        now = datetime.now(timezone.utc)
        active = []
        for e in entries:
            fresh = e.fresh_until
            if fresh.tzinfo is None:
                fresh = fresh.replace(tzinfo=timezone.utc)
            if fresh > now:
                active.append(e)

        evicted = len(entries) - len(active)
        if evicted:
            logger.info("Evicted %d expired entries from index", evicted)

        # Enforce cap: keep most recently published entries
        if len(active) > INDEX_CAP:
            active.sort(key=lambda e: e.published_at, reverse=True)
            dropped = len(active) - INDEX_CAP
            active = active[:INDEX_CAP]
            logger.info("Dropped %d entries to enforce index cap of %d", dropped, INDEX_CAP)

        # Build JSONL
        lines = [e.model_dump_json() for e in active]
        body = "\n".join(lines) + "\n" if lines else ""

        key = self._index_key()
        s3 = self._get_s3()
        s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
        logger.info("Saved index with %d entries to s3://%s/%s", len(active), self.bucket, key)

    def add_entry(self, entry: PublicKnowledgeIndexEntry) -> None:
        """Load index, append entry, evict expired, save."""
        entries = self.load_index()
        # Replace if doc_id already exists
        entries = [e for e in entries if e.doc_id != entry.doc_id]
        entries.append(entry)
        self.save_index(entries)

    def remove_entry(self, doc_id: str) -> None:
        """Load index, remove entry by doc_id, save."""
        entries = self.load_index()
        before = len(entries)
        entries = [e for e in entries if e.doc_id != doc_id]
        removed = before - len(entries)
        if removed:
            logger.info("Removed %d index entries for doc %s", removed, doc_id)
        else:
            logger.warning("No index entry found for doc %s", doc_id)
        self.save_index(entries)
