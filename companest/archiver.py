"""
Companest Memory Archiver

Backs up all team memory to S3 on a periodic schedule.
Managed by the Memory meta-team's archivist Pi.

Features:
- Full snapshot of .companest/ directory  S3
- Incremental change detection (only upload changed files)
- Configurable schedule (default: every 4 hours)
- Cleanup old backups (retention policy)
"""

import hashlib
import json
import logging
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .memory import MemoryManager
from .exceptions import ArchiverError

logger = logging.getLogger(__name__)


class MemoryArchiver:
    """
    Archives .companest/ memory to S3.

    Two modes:
    - snapshot: tar.gz the entire .companest/ directory
    - incremental: track file hashes, only upload changed files
    """

    def __init__(
        self,
        memory: MemoryManager,
        bucket: str,
        prefix: str = "companest-backup",
        region: str = "us-east-2",
        retention_days: int = 30,
    ):
        self.memory = memory
        self.bucket = bucket
        self.prefix = prefix
        self.region = region
        self.retention_days = retention_days
        self._s3 = None
        self._hash_file = Path(memory.base_path) / ".backup-hashes.json"
        self._last_hashes: Dict[str, str] = self._load_hashes()

    def _load_hashes(self) -> Dict[str, str]:
        """Load saved file hashes from disk."""
        if self._hash_file.exists():
            try:
                return json.loads(self._hash_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[Archiver] Failed to load hash file: {e}")
        return {}

    def _save_hashes(self) -> None:
        """Persist file hashes to disk."""
        try:
            self._hash_file.write_text(
                json.dumps(self._last_hashes, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[Archiver] Failed to save hash file: {e}")

    def _get_s3(self):
        """Lazy-init S3 client."""
        if self._s3 is None:
            try:
                import boto3
                self._s3 = boto3.client("s3", region_name=self.region)
            except ImportError:
                raise ArchiverError("boto3 not installed. Run: pip install boto3")
        return self._s3

    def _file_hash(self, path: Path) -> str:
        """SHA256 hash of a file."""
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    def _scan_files(self) -> Dict[str, str]:
        """Scan all files under .companest/ and return {relative_path: sha256}."""
        base = self.memory.base_path
        result = {}
        if not base.exists():
            return result
        for f in base.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                rel = str(f.relative_to(base))
                result[rel] = self._file_hash(f)
        return result

    def get_changed_files(self) -> List[str]:
        """Detect files that changed since last scan."""
        current = self._scan_files()
        changed = []
        for path, h in current.items():
            if self._last_hashes.get(path) != h:
                changed.append(path)
        # Detect deleted files
        deleted = set(self._last_hashes.keys()) - set(current.keys())
        self._last_hashes = current
        self._save_hashes()
        return changed

    async def backup_snapshot(self) -> Dict[str, Any]:
        """
        Create a full tar.gz snapshot and upload to S3.

        Returns metadata about the backup.
        """
        s3 = self._get_s3()
        base = self.memory.base_path
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        key = f"{self.prefix}/snapshots/{timestamp}.tar.gz"

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Create tar.gz
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(str(base), arcname=".companest")

            file_size = os.path.getsize(tmp_path)

            # Upload
            s3.upload_file(tmp_path, self.bucket, key)

            metadata = {
                "type": "snapshot",
                "timestamp": now.isoformat(),
                "s3_key": key,
                "size_bytes": file_size,
                "teams": self.memory.list_teams(),
            }

            # Record in memory team's backup log
            self.memory.append_team_memory("memory", "backup-log.json", metadata)

            logger.info(
                f"[Archiver] Snapshot uploaded: s3://{self.bucket}/{key} "
                f"({file_size:,} bytes)"
            )
            return metadata

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def backup_incremental(self) -> Dict[str, Any]:
        """
        Upload only changed files since last backup.

        Returns metadata about the backup.
        """
        s3 = self._get_s3()
        changed = self.get_changed_files()
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d-%H%M%S")

        if not changed:
            logger.info("[Archiver] No changes detected, skipping incremental backup")
            return {"type": "incremental", "timestamp": now.isoformat(), "files": 0}

        uploaded = []
        total_bytes = 0
        base = self.memory.base_path

        for rel_path in changed:
            full_path = base / rel_path
            if not full_path.exists():
                continue
            s3_key = f"{self.prefix}/incremental/{timestamp}/{rel_path}"
            s3.upload_file(str(full_path), self.bucket, s3_key)
            size = full_path.stat().st_size
            total_bytes += size
            uploaded.append(rel_path)

        metadata = {
            "type": "incremental",
            "timestamp": now.isoformat(),
            "files": len(uploaded),
            "total_bytes": total_bytes,
            "changed": uploaded,
        }

        self.memory.append_team_memory("memory", "backup-log.json", metadata)

        logger.info(
            f"[Archiver] Incremental backup: {len(uploaded)} files, "
            f"{total_bytes:,} bytes"
        )
        return metadata

    async def cleanup_old_backups(self) -> int:
        """
        Delete S3 snapshots older than retention_days.

        Returns number of objects deleted.
        """
        s3 = self._get_s3()
        prefix = f"{self.prefix}/snapshots/"
        cutoff = datetime.now(timezone.utc).timestamp() - (self.retention_days * 86400)

        paginator = s3.get_paginator("list_objects_v2")
        to_delete = []

        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"].timestamp() < cutoff:
                    to_delete.append({"Key": obj["Key"]})

        if to_delete:
            # Delete in batches of 1000 (S3 limit)
            for i in range(0, len(to_delete), 1000):
                batch = to_delete[i:i + 1000]
                s3.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": batch},
                )

            self.memory.append_team_memory("memory", "cleanup-log.json", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "deleted": len(to_delete),
                "retention_days": self.retention_days,
            })

        logger.info(f"[Archiver] Cleaned up {len(to_delete)} old backups")
        return len(to_delete)

    @staticmethod
    def _safe_tar_members(tar: tarfile.TarFile, target_path: str):
        """Filter tar members to prevent path traversal attacks."""
        target = Path(target_path).resolve()
        for member in tar.getmembers():
            member_path = (target / member.name).resolve()
            if not str(member_path).startswith(str(target) + os.sep) and member_path != target:
                logger.warning(f"[Archiver] Skipping unsafe tar member: {member.name}")
                continue
            # Block symlinks pointing outside target
            if member.issym() or member.islnk():
                link_target = (target / member.linkname).resolve()
                if not str(link_target).startswith(str(target) + os.sep):
                    logger.warning(f"[Archiver] Skipping symlink escaping target: {member.name} -> {member.linkname}")
                    continue
            yield member

    async def restore_snapshot(self, s3_key: str, target_path: str) -> None:
        """Restore a snapshot from S3 to a target directory."""
        s3 = self._get_s3()

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            s3.download_file(self.bucket, s3_key, tmp_path)
            with tarfile.open(tmp_path, "r:gz") as tar:
                safe_members = list(self._safe_tar_members(tar, target_path))
                tar.extractall(path=target_path, members=safe_members)
            logger.info(f"[Archiver] Restored {s3_key}  {target_path}")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def get_backup_history(self, limit: int = 20) -> List[Dict]:
        """Get recent backup history from memory."""
        log = self.memory.read_team_memory("memory", "backup-log.json")
        if not log or not isinstance(log, list):
            return []
        return log[-limit:]
