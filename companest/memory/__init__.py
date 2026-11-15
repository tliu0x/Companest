"""Companest Memory Subsystem"""

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
]
