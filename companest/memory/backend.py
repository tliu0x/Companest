"""
Companest Memory Backend  Abstract Contract

Defines the MemoryBackend ABC that all storage implementations must satisfy.
The default FileBackend wraps the existing MemoryManager via delegation,
preserving backward compatibility while enabling future backends
(Viking vector store, S3-backed, etc.).
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .manager import MemoryManager

logger = logging.getLogger(__name__)


class MemoryBackend(ABC):
    """
    Abstract contract for memory storage backends.

    Required methods mirror the MemoryManager's team-level API.
    Optional capabilities are declared via property flags so callers
    can adapt behavior without isinstance() checks.
    """

    #  Required operations 

    @abstractmethod
    def read(self, team_id: str, key: str) -> Any:
        """Read a memory entry. Returns None if not found."""
        ...

    @abstractmethod
    def write(self, team_id: str, key: str, data: Any) -> None:
        """Write (create or overwrite) a memory entry."""
        ...

    @abstractmethod
    def append(self, team_id: str, key: str, entry: Any) -> None:
        """Append an entry to a list-valued memory key."""
        ...

    @abstractmethod
    def list_keys(self, team_id: str) -> List[str]:
        """List all memory keys for a team."""
        ...

    @abstractmethod
    def delete(self, team_id: str, key: str) -> None:
        """Delete a memory entry."""
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
        """Move an entry to the archive."""
        ...

    @abstractmethod
    def search_archive(
        self, team_id: str, query: str, limit: int = 10,
    ) -> List[dict]:
        """Search archived entries by keyword."""
        ...

    @abstractmethod
    def read_overview(self, team_id: str) -> str:
        """Read the .overview.md for a team. Returns empty string if missing."""
        ...

    @abstractmethod
    def get_all_stats(self) -> Dict[str, Any]:
        """Get memory stats across all teams."""
        ...

    #  Optional capability flags 

    @property
    def supports_semantic_search(self) -> bool:
        """Whether this backend supports vector/semantic search natively."""
        return False

    @property
    def supports_native_compaction(self) -> bool:
        """Whether this backend handles compaction internally."""
        return False

    @property
    def supports_snapshot(self) -> bool:
        """Whether this backend supports CoW snapshots."""
        return False

    def export_snapshot(self, team_id: str) -> Dict[str, Any]:
        """Export a snapshot of team memory for backup/migration."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support snapshots"
        )

    def restore_snapshot(self, team_id: str, snapshot: Dict[str, Any]) -> None:
        """Restore a team's memory from a snapshot."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support snapshots"
        )

    def semantic_search(
        self,
        team_id: str,
        query: str,
        *,
        limit: int = 20,
        include_archive: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Vector similarity search over memory entries.

        Only callable when supports_semantic_search is True.
        Each result dict must contain at least 'key', 'score', and 'source'.

        Raises:
            NotImplementedError: If the backend does not support semantic search.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support semantic search"
        )


class FileBackend(MemoryBackend):
    """
    Thin adapter that delegates to the existing MemoryManager.

    This is the default backend. It does NOT extract code from manager.py;
    it simply wraps it so that higher-level services can program against
    the MemoryBackend interface.
    """

    def __init__(self, manager: MemoryManager) -> None:
        self._mgr = manager

    @property
    def manager(self) -> MemoryManager:
        """Access the underlying MemoryManager (e.g. for prompt building)."""
        return self._mgr

    #  Required operations (pure delegation) 

    def read(self, team_id: str, key: str) -> Any:
        return self._mgr.read_team_memory(team_id, key)

    def write(self, team_id: str, key: str, data: Any) -> None:
        self._mgr.write_team_memory(team_id, key, data)

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
        self, team_id: str, query: str, limit: int = 10,
    ) -> List[dict]:
        return self._mgr.search_archive(team_id, query, limit)

    def read_overview(self, team_id: str) -> str:
        return self._mgr.read_overview(team_id)

    def get_all_stats(self) -> Dict[str, Any]:
        return self._mgr.get_all_memory_stats()

    #  Optional capabilities 

    @property
    def supports_snapshot(self) -> bool:
        return True

    def export_snapshot(self, team_id: str) -> Dict[str, Any]:
        """Export all memory entries + index as a dict snapshot."""
        index = self._mgr.get_memory_index(team_id)
        entries: Dict[str, Any] = {}
        for key in self._mgr.list_team_memory(team_id):
            entries[key] = self._mgr.read_team_memory(team_id, key)
        return {"index": index, "entries": entries}

    def restore_snapshot(self, team_id: str, snapshot: Dict[str, Any]) -> None:
        """Restore entries from a snapshot dict."""
        entries = snapshot.get("entries", {})
        for key, data in entries.items():
            self._mgr.write_team_memory(team_id, key, data)
        logger.info(
            "Restored snapshot for team %s: %d entries", team_id, len(entries),
        )
