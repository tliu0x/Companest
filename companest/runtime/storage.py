"""Identity checks and locks for mounted business files.

Shared locks use atomic hard-link creation: flock inside a gVisor sandbox does
not exclude another sandbox or a native control process. Never expire a claim
by age; a suspended actor can still own it. Substrate-confirmed deletion permits
cleanup of that job's claims. Local SQLite ownership uses a native kernel lock.
"""

from contextvars import ContextVar
import errno
import fcntl
import json
import os
from pathlib import Path
import threading
import time
import uuid

LOCK_OWNER: ContextVar[dict] = ContextVar("storage_lock_owner", default={})
LOCK_PROTOCOL = "hardlink-v1"


def check_identity(directory: Path, expected: str) -> None:
    try:
        actual = (directory / ".companest-storage-id").read_text().strip()
    except OSError as exc:
        raise ValueError(f"Persistent storage identity is missing at {directory}") from exc
    if not expected or actual != expected:
        raise ValueError(f"Persistent storage identity mismatch at {directory}")


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalFileLock:
    """Native process ownership for local/block control storage only."""

    def __init__(self, path: Path, *, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle = None

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def _release(self):
        self._handle.close()
        self._handle = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            if not self._depth:
                self._acquire()
            self._depth += 1
            return self
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, *exc):
        try:
            self._depth -= 1
            if not self._depth:
                self._release()
        finally:
            self._thread_lock.release()


class FileLock(LocalFileLock):
    """Reentrant shared-storage claim with bounded, fail-closed contention.

    Publish complete owner metadata atomically with link(), avoiding a crash
    window between claiming a lock and recording its owner. A holder killed by
    its runtime leaves a claim, which only confirmed retirement may remove.
    """

    def __init__(self, path: Path, *, blocking: bool = True, timeout: float = 30):
        super().__init__(path, blocking=blocking)
        self.claim = path.with_name(path.name + ".claim")
        self.timeout = timeout
        self._token = None

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        candidate = self.path.with_name(f".{self.path.name}-{token}.owner")
        owner = {**LOCK_OWNER.get(), "token": token, "pid": os.getpid(), "created_at": time.time()}
        deadline = time.monotonic() + self.timeout
        try:
            with candidate.open("x") as stream:
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
            while True:
                try:
                    os.link(candidate, self.claim)
                    self._token = token
                    _sync(self.path.parent)
                    return
                except FileExistsError:
                    if not self.blocking:
                        raise BlockingIOError(errno.EWOULDBLOCK, "Shared storage lock is held", str(self.claim))
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Shared storage lock is held: {self.claim}; verify its owner before recovery")
                    time.sleep(0.05)
        except BaseException:
            # link/fsync can fail with an uncertain outcome. Remove only our
            # own published claim; no protected operation has started yet.
            try:
                if json.loads(self.claim.read_text()).get("token") == token:
                    self.claim.unlink()
                    _sync(self.path.parent)
            except (OSError, ValueError):
                pass
            self._token = None
            raise
        finally:
            candidate.unlink(missing_ok=True)

    def _release(self):
        owner = json.loads(self.claim.read_text())
        if owner.get("token") != self._token:
            raise RuntimeError("Shared storage lock ownership changed; refusing to release it")
        self.claim.unlink()
        _sync(self.path.parent)
        self._token = None


def release_job_locks(directory: Path, job_id: str) -> None:
    """Caller must first confirm this job's actor was deleted, never just idle.

    Serialize cleanup with the job's control-plane lock. Unknown/control-process
    claims are deliberately retained; an operator must establish owner death.
    """
    for claim in directory.glob("*.claim"):
        try:
            owner = json.loads(claim.read_text())
        except FileNotFoundError:
            continue
        if owner.get("job_id") == job_id:
            claim.unlink()
            _sync(directory)
