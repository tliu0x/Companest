from abc import ABC, abstractmethod
from typing import Any, Dict, List

from .manager import MemoryManager


class MemoryBackend(ABC):
    """Abstract contract for memory storage backends."""

    @abstractmethod
    def read(self, team_id: str, key: str) -> Any:
        """Read a memory entry. Returns None if not found."""
        ...

    @abstractmethod
    def write(self, team_id: str, key: str, data: Any, **metadata) -> int:
        ...

    @abstractmethod
    def append(self, team_id: str, key: str, entry: Any) -> None:
        """Append an entry to a list-valued memory key."""
        ...

    @abstractmethod
    def list_keys(self, team_id: str) -> List[str]:
        ...

    @abstractmethod
    def delete(self, team_id: str, key: str) -> None:
        ...

    @abstractmethod
    def get_index(self, team_id: str) -> Dict[str, dict]:
        """Return the full inode-like metadata index for a team."""
        ...

    @abstractmethod
    def update_meta(self, team_id: str, key: str, **fields) -> None:
        """Update metadata fields (importance, tier, summary, tags)."""
        ...

    @abstractmethod
    def archive(self, team_id: str, key: str) -> None:
        ...

    @abstractmethod
    def search_archive(
        self, team_id: str, query: str, limit: int = 10, *, category: str | None = None,
    ) -> List[dict]:
        ...

    @abstractmethod
    def read_overview(self, team_id: str) -> str:
        """Read the .overview.md for a team. Returns empty string if missing."""
        ...

    @abstractmethod
    def get_all_stats(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def read_entry(self, team_id: str, key: str, **options) -> dict:
        ...

    @abstractmethod
    def list_versions(self, team_id: str, key: str, **options) -> List[dict]:
        ...

    @abstractmethod
    def restore(self, team_id: str, key: str, version: int, *, expected_version: int) -> int:
        ...


class FileBackend(MemoryBackend):
    """Thin adapter that delegates to MemoryManager."""

    def __init__(self, manager: MemoryManager) -> None:
        self._mgr = manager

    def read(self, team_id: str, key: str) -> Any:
        return self._mgr.read_team_memory(team_id, key)

    def write(self, team_id: str, key: str, data: Any, **metadata) -> int:
        return self._mgr.write_team_memory(team_id, key, data, **metadata)

    def append(self, team_id: str, key: str, entry: Any) -> None:
        self._mgr.append_team_memory(team_id, key, entry)

    def list_keys(self, team_id: str) -> List[str]:
        return self._mgr.list_team_memory(team_id)

    def delete(self, team_id: str, key: str) -> None:
        self._mgr.delete_team_memory(team_id, key)

    def get_index(self, team_id: str) -> Dict[str, dict]:
        return self._mgr.get_memory_index(team_id)

    def update_meta(self, team_id: str, key: str, **fields) -> None:
        self._mgr.update_entry_meta(team_id, key, **fields)

    def archive(self, team_id: str, key: str) -> None:
        self._mgr.archive_team_memory(team_id, key)

    def search_archive(
        self, team_id: str, query: str, limit: int = 10, *, category: str | None = None,
    ) -> List[dict]:
        return self._mgr.search_archive(team_id, query, limit, category=category)

    def read_overview(self, team_id: str) -> str:
        return self._mgr.read_overview(team_id)

    def get_all_stats(self) -> Dict[str, Any]:
        return self._mgr.get_all_memory_stats()

    def read_entry(self, team_id: str, key: str, **options) -> dict:
        return self._mgr.read_memory_entry(team_id, key, **options)

    def list_versions(self, team_id: str, key: str, **options) -> List[dict]:
        return self._mgr.list_memory_versions(team_id, key, **options)

    def restore(self, team_id: str, key: str, version: int, *, expected_version: int) -> int:
        return self._mgr.restore_memory_version(team_id, key, version, expected_version=expected_version)
