"""
Companest S3 Sync  Memory Snapshot Sync to S3

Syncs memory snapshots to/from S3-compatible object storage at the
team-entry level (not system-wide tar.gz like MemoryArchiver).

Use cases:
- Disaster recovery with point-in-time restore per team
- Cross-instance migration (move a team's memory to another deployment)
- Periodic entry-level backup (complementary to MemoryArchiver snapshots)

Requires a MemoryBackend that supports snapshots
(supports_snapshot == True).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .backend import MemoryBackend

logger = logging.getLogger(__name__)


class S3SyncConfig:
    """Configuration for S3 sync."""

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "companest-memory/",
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
        sync_interval_seconds: int = 3600,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.region = region
        self.endpoint_url = endpoint_url
        self.sync_interval_seconds = sync_interval_seconds


class S3Sync:
    """
    Sync memory snapshots to S3-compatible storage.

    Operates at the team level: each team's memory is serialized to a
    JSON snapshot and uploaded as a single S3 object. This complements
    the system-level MemoryArchiver (tar.gz of entire .companest/).
    """

    def __init__(
        self,
        backend: MemoryBackend,
        config: Optional[S3SyncConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or S3SyncConfig()
        self._s3 = None

        if not self._backend.supports_snapshot:
            logger.warning(
                "Backend %s does not support snapshots; "
                "S3Sync will not be able to export/restore.",
                type(self._backend).__name__,
            )

    def _get_s3(self):
        """Lazy-init S3 client."""
        if self._s3 is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for S3Sync. "
                    "Install with: pip install companest-orchestrator[s3]"
                )
            kwargs: Dict[str, Any] = {"region_name": self._config.region}
            if self._config.endpoint_url:
                kwargs["endpoint_url"] = self._config.endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _team_key(self, team_id: str, timestamp: str) -> str:
        """Build the S3 object key for a team snapshot."""
        return f"{self._config.prefix}{team_id}/{timestamp}.json"

    def upload_snapshot(self, team_id: str) -> str:
        """
        Export a snapshot from the backend and upload to S3.

        Returns:
            The S3 object key for the uploaded snapshot.
        """
        snapshot = self._backend.export_snapshot(team_id)
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        key = self._team_key(team_id, timestamp)

        body = json.dumps(
            {"team_id": team_id, "timestamp": now.isoformat(), "snapshot": snapshot},
            ensure_ascii=False,
            default=str,
        )

        s3 = self._get_s3()
        s3.put_object(
            Bucket=self._config.bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

        logger.info(
            "Uploaded snapshot for team %s  s3://%s/%s (%d bytes)",
            team_id, self._config.bucket, key, len(body),
        )
        return key

    def download_snapshot(self, team_id: str, s3_key: str) -> Dict[str, Any]:
        """
        Download a snapshot from S3.

        Returns:
            The snapshot dict (contains 'team_id', 'timestamp', 'snapshot').
        """
        s3 = self._get_s3()
        resp = s3.get_object(Bucket=self._config.bucket, Key=s3_key)
        body = resp["Body"].read().decode("utf-8")
        data = json.loads(body)
        logger.info(
            "Downloaded snapshot from s3://%s/%s for team %s",
            self._config.bucket, s3_key, team_id,
        )
        return data

    def restore_from_s3(self, team_id: str, s3_key: str) -> None:
        """
        Download a snapshot from S3 and restore it to the backend.
        """
        data = self.download_snapshot(team_id, s3_key)
        snapshot = data.get("snapshot", data)
        self._backend.restore_snapshot(team_id, snapshot)
        logger.info(
            "Restored team %s from s3://%s/%s",
            team_id, self._config.bucket, s3_key,
        )

    def sync_all_teams(self) -> Dict[str, str]:
        """
        Export and upload snapshots for all teams.

        Returns:
            Dict mapping team_id to S3 object key.
        """
        stats = self._backend.get_all_stats()
        results: Dict[str, str] = {}

        for team_id in stats:
            try:
                key = self.upload_snapshot(team_id)
                results[team_id] = key
            except Exception as e:
                logger.error(
                    "Failed to sync team %s: %s", team_id, e,
                )

        logger.info("Synced %d/%d teams", len(results), len(stats))
        return results

    def list_snapshots(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List available snapshots in S3, optionally filtered by team.

        Returns:
            List of dicts with 'key', 'team_id', 'last_modified', 'size'.
        """
        s3 = self._get_s3()
        prefix = self._config.prefix
        if team_id:
            prefix = f"{self._config.prefix}{team_id}/"

        paginator = s3.get_paginator("list_objects_v2")
        snapshots: List[Dict[str, Any]] = []

        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Extract team_id from key: prefix/<team_id>/<timestamp>.json
                parts = key[len(self._config.prefix):].split("/")
                tid = parts[0] if parts else ""
                snapshots.append({
                    "key": key,
                    "team_id": tid,
                    "last_modified": obj["LastModified"].isoformat(),
                    "size": obj["Size"],
                })

        snapshots.sort(key=lambda s: s["last_modified"], reverse=True)
        return snapshots
