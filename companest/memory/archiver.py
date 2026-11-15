import logging
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from typing import Any, Dict

from . import MemoryManager
from ..exceptions import ArchiverError

logger = logging.getLogger(__name__)


class MemoryArchiver:
    """Archives .companest/ memory to S3."""

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

    def _get_s3(self):
        if self._s3 is None:
            try:
                import boto3
                self._s3 = boto3.client("s3", region_name=self.region)
            except ImportError:
                raise ArchiverError("boto3 not installed. Run: pip install boto3")
        return self._s3

    async def backup_snapshot(self) -> Dict[str, Any]:
        """Create a full tar.gz snapshot and upload to S3."""
        s3 = self._get_s3()
        base = self.memory.base_path
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        key = f"{self.prefix}/snapshots/{timestamp}.tar.gz"

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(str(base), arcname=".companest")

            file_size = os.path.getsize(tmp_path)

            s3.upload_file(tmp_path, self.bucket, key)

            metadata = {
                "type": "snapshot",
                "timestamp": now.isoformat(),
                "s3_key": key,
                "size_bytes": file_size,
                "teams": self.memory.list_teams(),
            }

            self.memory.append_team_memory("memory", "backup-log.json", metadata)

            logger.info(
                f"[Archiver] Snapshot uploaded: s3://{self.bucket}/{key} "
                f"({file_size:,} bytes)"
            )
            return metadata

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def restore_snapshot(self, s3_key: str, target_path: str) -> Path:
        """Restore into a new target directory and return its .companest path."""
        target = Path(target_path).absolute()
        if target.exists() or target.is_symlink():
            raise ArchiverError(f"Restore target already exists: {target}")
        if target.resolve().is_relative_to(self.memory.base_path.resolve()):
            raise ArchiverError("Restore target must be outside the active memory directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".companest-restore-", dir=target.parent) as work:
            work_path = Path(work)
            archive = work_path / "snapshot.tar.gz"
            staging = work_path / "extracted"
            try:
                self._get_s3().download_file(self.bucket, s3_key, str(archive))
                with tarfile.open(archive, "r:gz") as tar:
                    members = tar.getmembers()
                    for member in members:
                        path = PurePosixPath(member.name)
                        if (path.is_absolute() or ".." in path.parts
                                or not path.parts or path.parts[0] != ".companest"
                                or not (member.isfile() or member.isdir())):
                            raise ArchiverError(f"Unsafe snapshot member: {member.name}")
                    tar.extractall(staging, members=members, filter="data")
                restored = staging / ".companest"
                if not restored.is_dir():
                    raise ArchiverError("Snapshot has no .companest directory")
                target.mkdir()
                try:
                    restored.rename(target / ".companest")
                except BaseException:
                    target.rmdir()
                    raise
            except (OSError, tarfile.TarError) as e:
                raise ArchiverError(f"Snapshot restore failed: {e}") from e
        return target / ".companest"
