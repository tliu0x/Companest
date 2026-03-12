"""
Companest Memory Subsystem

Hierarchical memory manager with OS-inspired consolidation:
- MemoryManager: file-based memory with inode index and three-layer cache
- Dreamer: scheduled importance scoring, compaction, GC, CoW snapshots
- MemoryBackend / FileBackend: pluggable storage abstraction
- QdrantBackend: hybrid file + vector search backend (replaces VikingBackend)
- MemorySearchService: exact-first search with capability-aware semantic fallback
- S3Sync: team-level snapshot sync to S3
"""

from .manager import (
    MemoryManager,
    MemoryError,
    MemoryEntryMeta,
    EnrichmentSource,
    INDEX_FILENAME,
)
from .dreamer import Dreamer, DreamerError
from .backend import MemoryBackend, FileBackend
from .search import MemorySearchService
from .qdrant_backend import QdrantBackend
from .s3_sync import S3Sync, S3SyncConfig

# Backward compatibility  VikingBackend is deprecated in favor of QdrantBackend
from .viking_backend import VikingBackend

__all__ = [
    "MemoryManager",
    "MemoryError",
    "MemoryEntryMeta",
    "EnrichmentSource",
    "INDEX_FILENAME",
    "Dreamer",
    "DreamerError",
    "MemoryBackend",
    "FileBackend",
    "QdrantBackend",
    "MemorySearchService",
    "S3Sync",
    "S3SyncConfig",
    # Deprecated
    "VikingBackend",
]
