"""
Companest Viking Backend -Experimental Vector Store Backend

Stub implementation for a future vector-database-backed memory backend
(e.g., Vespa/Viking, Pinecone, Weaviate). This backend would provide
native semantic search and potentially native compaction.

Status: NOT IMPLEMENTED. All required methods raise NotImplementedError.
Capability flags stay disabled until a real implementation lands so
callers do not treat this backend as production-ready.
"""

import logging
from typing import Any, Dict, List

from .backend import MemoryBackend

logger = logging.getLogger(__name__)


class VikingBackend(MemoryBackend):
    """
    Experimental vector-store memory backend.

    Would provide:
    - Native embedding + ANN semantic search
    - Automatic compaction via vector clustering
    - Snapshot export to portable format

    All methods currently raise NotImplementedError.
    Capability flags are intentionally disabled until that changes.
    """

    def __init__(self, endpoint: str = "", api_key: str = "") -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        logger.warning(
            "VikingBackend is experimental and not yet implemented. "
            "Do not use in production."
        )

    # -- Capability flags ---------------------------------------

    @property
    def supports_semantic_search(self) -> bool:
        return False

    @property
    def supports_native_compaction(self) -> bool:
        return False

    @property
    def supports_snapshot(self) -> bool:
        return False

    # -- Required operations (all stubs) ------------------------

    def read(self, team_id: str, key: str) -> Any:
        raise NotImplementedError("VikingBackend.read is not yet implemented")

    def write(self, team_id: str, key: str, data: Any) -> None:
        raise NotImplementedError("VikingBackend.write is not yet implemented")

    def append(self, team_id: str, key: str, entry: Any) -> None:
        raise NotImplementedError("VikingBackend.append is not yet implemented")

    def list_keys(self, team_id: str) -> List[str]:
        raise NotImplementedError("VikingBackend.list_keys is not yet implemented")

    def delete(self, team_id: str, key: str) -> None:
        raise NotImplementedError("VikingBackend.delete is not yet implemented")

    def get_index(self, team_id: str) -> Dict[str, dict]:
        raise NotImplementedError("VikingBackend.get_index is not yet implemented")

    def update_meta(self, team_id: str, key: str, **fields) -> None:
        raise NotImplementedError("VikingBackend.update_meta is not yet implemented")

    def archive(self, team_id: str, key: str) -> None:
        raise NotImplementedError("VikingBackend.archive is not yet implemented")

    def search_archive(
        self, team_id: str, query: str, limit: int = 10,
    ) -> List[dict]:
        raise NotImplementedError("VikingBackend.search_archive is not yet implemented")

    def read_overview(self, team_id: str) -> str:
        raise NotImplementedError("VikingBackend.read_overview is not yet implemented")

    def get_all_stats(self) -> Dict[str, Any]:
        raise NotImplementedError("VikingBackend.get_all_stats is not yet implemented")

    # -- Optional operations (stubs) ----------------------------

    def export_snapshot(self, team_id: str) -> Dict[str, Any]:
        raise NotImplementedError(
            "VikingBackend.export_snapshot is not yet implemented"
        )

    def restore_snapshot(self, team_id: str, snapshot: Dict[str, Any]) -> None:
        raise NotImplementedError(
            "VikingBackend.restore_snapshot is not yet implemented"
        )
