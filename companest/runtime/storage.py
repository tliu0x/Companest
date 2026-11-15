"""Locks and identity checks for operator-mounted persistent filesystems.

Substrate owns mounts. These locks coordinate business file updates, not actor
placement. Mounts must support POSIX advisory locks (for example NFSv4).
"""

import fcntl
from pathlib import Path
import threading


def check_identity(directory: Path, expected: str) -> None:
    try:
        actual = (directory / ".companest-storage-id").read_text().strip()
    except OSError as exc:
        raise ValueError(f"Persistent storage identity is missing at {directory}") from exc
    if not expected or actual != expected:
        raise ValueError(f"Persistent storage identity mismatch at {directory}")


class FileLock:
    """Thread-reentrant, process-shared lock; never unlink a live lock file."""

    def __init__(self, path: Path, *, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            if not self._depth:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.open("a+")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
                except BaseException:
                    handle.close()
                    raise
                self._handle = handle
            self._depth += 1
            return self
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, *exc):
        self._depth -= 1
        if not self._depth:
            self._handle.close()
            self._handle = None
        self._thread_lock.release()
