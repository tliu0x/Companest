"""
S3-backed document storage for Public Knowledge.

Stores each document as a JSON file at docs/{doc_id}.json under
the configured S3 prefix (default: companest-public-knowledge/).

Follows the lazy boto3 init pattern from companest.memory.s3_sync.
"""

import json
import logging
from typing import Any, Dict, Optional

from .models import PublicKnowledgeDoc

logger = logging.getLogger(__name__)


class PublicKnowledgeS3Store:
    """
    S3-backed storage for public knowledge documents.

    Each document is stored as a JSON object at:
        s3://{bucket}/{prefix}docs/{doc_id}.json
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
                    "boto3 is required for PublicKnowledgeS3Store. "
                    "Install with: pip install companest-orchestrator[s3]"
                )
            kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _doc_key(self, doc_id: str) -> str:
        """Build the S3 object key for a document."""
        return f"{self.prefix}docs/{doc_id}.json"

    def put_doc(self, doc: PublicKnowledgeDoc) -> str:
        """
        Write a document to S3 as JSON.

        Returns:
            The S3 object key for the uploaded document.
        """
        key = self._doc_key(doc.doc_id)
        body = doc.model_dump_json(indent=2)

        s3 = self._get_s3()
        s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

        logger.info(
            "Put doc %s -> s3://%s/%s (%d bytes)",
            doc.doc_id, self.bucket, key, len(body),
        )
        return key

    def get_doc(self, doc_id: str) -> Optional[PublicKnowledgeDoc]:
        """
        Read a document from S3.

        Returns:
            PublicKnowledgeDoc if found, None if not found.
        """
        key = self._doc_key(doc_id)
        s3 = self._get_s3()

        try:
            resp = s3.get_object(Bucket=self.bucket, Key=key)
            body = resp["Body"].read().decode("utf-8")
            data = json.loads(body)
            doc = PublicKnowledgeDoc(**data)
            logger.info("Get doc %s from s3://%s/%s", doc_id, self.bucket, key)
            return doc
        except s3.exceptions.NoSuchKey:
            logger.warning("Doc not found: s3://%s/%s", self.bucket, key)
            return None
        except Exception as e:
            logger.error("Failed to get doc %s: %s", doc_id, e)
            raise

    def delete_doc(self, doc_id: str) -> None:
        """Remove a document from S3."""
        key = self._doc_key(doc_id)
        s3 = self._get_s3()

        s3.delete_object(Bucket=self.bucket, Key=key)
        logger.info("Deleted doc %s from s3://%s/%s", doc_id, self.bucket, key)
