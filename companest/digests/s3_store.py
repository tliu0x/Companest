"""
Companest Digest S3 Store

Stores and retrieves company-private digests in S3-compatible storage.
Follows the lazy-init boto3 pattern from companest.memory.s3_sync.

S3 key layout:
    companest-digests/{company_id}/{digest_type}/{idempotency_key}.json
    companest-digests/{company_id}/{digest_type}/latest.json  (pointer)
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DigestS3Store:
    """
    S3-backed storage for company-private digests.

    Uses lazy boto3 initialization so the module can be imported without
    boto3 installed (tests can mock the client).
    """

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "companest-digests/",
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        self._region = region
        self._endpoint_url = endpoint_url
        self._s3 = None

    def _get_s3(self):
        """Lazy-init S3 client."""
        if self._s3 is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for DigestS3Store. "
                    "Install with: pip install companest-orchestrator[s3]"
                )
            kwargs: Dict[str, Any] = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _build_key(
        self, company_id: str, digest_type: str, filename: str
    ) -> str:
        """Build an S3 object key."""
        return f"{self._prefix}{company_id}/{digest_type}/{filename}"

    def put_digest(
        self,
        company_id: str,
        digest_type: str,
        digest_data: Dict[str, Any],
        idempotency_key: str,
    ) -> str:
        """
        Upload a digest payload and update the latest pointer.

        Returns:
            The S3 object key for the uploaded digest.
        """
        s3 = self._get_s3()
        body = json.dumps(digest_data, ensure_ascii=False, default=str)

        # Store the versioned object
        key = self._build_key(company_id, digest_type, f"{idempotency_key}.json")
        s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

        # Update latest pointer
        latest_key = self._build_key(company_id, digest_type, "latest.json")
        s3.put_object(
            Bucket=self._bucket,
            Key=latest_key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

        logger.info(
            "Stored digest s3://%s/%s (%d bytes)",
            self._bucket, key, len(body),
        )
        return key

    def get_latest_digest(
        self, company_id: str, digest_type: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve the latest digest for a company + digest_type.

        Returns:
            The digest dict, or None if no digest exists.
        """
        s3 = self._get_s3()
        latest_key = self._build_key(company_id, digest_type, "latest.json")

        try:
            resp = s3.get_object(Bucket=self._bucket, Key=latest_key)
            body = resp["Body"].read().decode("utf-8")
            return json.loads(body)
        except s3.exceptions.NoSuchKey:
            return None
        except Exception as e:
            # ClientError with 404 code
            error_code = getattr(
                getattr(e, "response", None), "get", lambda *_: {}
            )("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                return None
            logger.error(
                "Failed to get latest digest %s/%s: %s",
                company_id, digest_type, e,
            )
            raise

    def list_digests(
        self, company_id: str, digest_type: str, limit: int = 10
    ) -> List[str]:
        """
        List S3 keys for digests of a given company + type.

        Returns:
            List of S3 object keys (most recent first), excluding latest.json.
        """
        s3 = self._get_s3()
        prefix = self._build_key(company_id, digest_type, "")

        paginator = s3.get_paginator("list_objects_v2")
        keys: List[str] = []

        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Skip the latest pointer
                if key.endswith("/latest.json"):
                    continue
                keys.append(key)

        # Sort by key descending (newest first assuming idempotency keys sort)
        keys.sort(reverse=True)
        return keys[:limit]
