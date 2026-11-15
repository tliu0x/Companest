"""Shared S3 JSON storage for the knowledge subsystem."""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class S3JsonStore:
    """Minimal S3 object store with a lazily created boto3 client."""

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "",
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.region = region
        self.endpoint_url = endpoint_url or None
        self._s3 = None

    def _get_s3(self):
        if self._s3 is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    f"boto3 is required for {type(self).__name__}. "
                    "Install with: pip install companest-orchestrator[s3]"
                )
            kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _put(self, key: str, body: str, content_type: str = "application/json") -> str:
        full_key = self.prefix + key
        self._get_s3().put_object(
            Bucket=self.bucket,
            Key=full_key,
            Body=body.encode("utf-8"),
            ContentType=content_type,
        )
        logger.info("Stored s3://%s/%s (%d bytes)", self.bucket, full_key, len(body))
        return full_key

    def _get(self, key: str) -> Optional[str]:
        """Read a text object; return None when the key does not exist."""
        try:
            resp = self._get_s3().get_object(Bucket=self.bucket, Key=self.prefix + key)
        except Exception as e:
            if _is_missing_key(e):
                return None
            raise
        return resp["Body"].read().decode("utf-8")

    def _get_json(self, key: str) -> Optional[Any]:
        body = self._get(key)
        return None if body is None else json.loads(body)

    def _delete(self, key: str) -> None:
        self._get_s3().delete_object(Bucket=self.bucket, Key=self.prefix + key)


def _is_missing_key(error: Exception) -> bool:
    if type(error).__name__ == "NoSuchKey":
        return True
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    return code in ("NoSuchKey", "404")
