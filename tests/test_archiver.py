import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from companest.exceptions import ArchiverError
from companest.memory import MemoryManager
from companest.memory.archiver import MemoryArchiver


@pytest.fixture
def archiver(tmp_path):
    base = tmp_path / "source"
    base.mkdir()
    instance = MemoryArchiver(MemoryManager(str(base)), bucket="test-bucket")
    instance._s3 = MagicMock()
    return instance


@pytest.mark.asyncio
async def test_snapshot_round_trip_to_new_directory(archiver, tmp_path):
    memory = archiver.memory
    memory.write_team_memory("alpha", "notes.json", {"note": "saved"})
    (memory.base_path / "config.md").write_text("configuration")
    objects = {}

    def upload(path, bucket, key):
        assert bucket == "test-bucket"
        objects[key] = Path(path).read_bytes()

    def download(bucket, key, path):
        assert bucket == "test-bucket"
        Path(path).write_bytes(objects[key])

    archiver._s3.upload_file.side_effect = upload
    archiver._s3.download_file.side_effect = download
    metadata = await archiver.backup_snapshot()
    memory.write_team_memory("alpha", "notes.json", {"note": "newer live value"})
    target = tmp_path / "restored"
    restored = await archiver.restore_snapshot(metadata["s3_key"], str(target))
    assert restored == target / ".companest"
    assert MemoryManager(str(restored)).read_team_memory("alpha", "notes.json") == {"note": "saved"}
    assert (restored / "config.md").read_text() == "configuration"
    assert memory.read_team_memory("alpha", "notes.json") == {"note": "newer live value"}
    with pytest.raises(ArchiverError, match="already exists"):
        await archiver.restore_snapshot(metadata["s3_key"], str(target))
    archiver._s3.download_file.assert_called_once()
    assert not list(tmp_path.glob(".companest-restore-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind", [
    ("../escaped", tarfile.REGTYPE),
    ("/absolute", tarfile.REGTYPE),
    (".companest/../../escaped", tarfile.REGTYPE),
    ("other/file", tarfile.REGTYPE),
    (".companest/link", tarfile.SYMTYPE),
    (".companest/hardlink", tarfile.LNKTYPE),
    (".companest/pipe", tarfile.FIFOTYPE),
])
async def test_restore_rejects_unsafe_members(archiver, tmp_path, name, kind):
    def download(bucket, key, path):
        with tarfile.open(path, "w:gz") as tar:
            good = tarfile.TarInfo(".companest/good")
            good.size = 2
            tar.addfile(good, io.BytesIO(b"ok"))
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "../../escaped"
            tar.addfile(member)

    archiver._s3.download_file.side_effect = download
    target = tmp_path / "restored"
    with pytest.raises(ArchiverError, match="Unsafe snapshot"):
        await archiver.restore_snapshot("snapshot.tar.gz", str(target))
    assert not target.exists()
    assert not (tmp_path / "escaped").exists()
    assert not list(tmp_path.glob(".companest-restore-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["download", "invalid_archive", "empty_archive"])
async def test_failed_restore_cleans_staging_and_allows_retry(archiver, tmp_path, failure):
    def download(bucket, key, path):
        Path(path).write_bytes(b"partial")
        if failure == "download":
            raise OSError("download interrupted")
        if failure == "empty_archive":
            with tarfile.open(path, "w:gz"):
                pass

    archiver._s3.download_file.side_effect = download
    target = tmp_path / "restored"
    for attempt in range(2):
        with pytest.raises(ArchiverError):
            await archiver.restore_snapshot("snapshot.tar.gz", str(target))
        assert not target.exists()
        assert not list(tmp_path.glob(".companest-restore-*"))


@pytest.mark.asyncio
async def test_restore_rejects_target_inside_active_memory(archiver):
    target = archiver.memory.base_path / "restore"
    with pytest.raises(ArchiverError, match="outside the active memory"):
        await archiver.restore_snapshot("snapshot.tar.gz", str(target))
    assert not target.exists()
    archiver._s3.download_file.assert_not_called()


@pytest.mark.asyncio
async def test_restore_preserves_target_created_during_download(archiver, tmp_path):
    target = tmp_path / "restore"

    def download(bucket, key, path):
        with tarfile.open(path, "w:gz") as tar:
            member = tarfile.TarInfo(".companest")
            member.type = tarfile.DIRTYPE
            tar.addfile(member)
        target.mkdir()
        (target / "existing").write_text("keep")

    archiver._s3.download_file.side_effect = download
    with pytest.raises(ArchiverError):
        await archiver.restore_snapshot("snapshot.tar.gz", str(target))
    assert (target / "existing").read_text() == "keep"
    assert not (target / ".companest").exists()
    assert not list(tmp_path.glob(".companest-restore-*"))
