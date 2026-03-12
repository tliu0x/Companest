"""
Companest Qdrant Backend  Vector Store Memory Backend

Wraps a FileBackend for durable read/write and layers Qdrant on top for
semantic search. Every write/append/delete synchronizes the vector index.

Requires:
- qdrant-client >= 1.9.0
- A running Qdrant instance (or uses in-memory/local for dev)

The backend embeds memory entry summaries + key names using a lightweight
embedding model via Qdrant's built-in FastEmbed integration, so no separate
embedding service is needed.
"""

import logging
from typing import Any, Dict, List, Optional

from .backend import MemoryBackend, FileBackend
from .manager import MemoryManager

logger = logging.getLogger(__name__)

# Qdrant collection naming: companest_memory_{team_id}
_COLLECTION_PREFIX = "companest_memory_"
_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_VECTOR_SIZE = 384  # bge-small-en-v1.5 output dimension


class QdrantBackend(MemoryBackend):
    """
    Hybrid memory backend: FileBackend for storage + Qdrant for semantic search.

    Data flow:
    - read/write/append/delete  delegated to FileBackend (files remain source of truth)
    - write/append/delete also update the Qdrant vector index
    - semantic_search  Qdrant similarity query
    - exact search (get_index, search_archive)  delegated to FileBackend
    """

    def __init__(
        self,
        manager: MemoryManager,
        qdrant_url: str = "http://localhost:6333",
        qdrant_api_key: Optional[str] = None,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        prefer_grpc: bool = False,
        in_memory: bool = False,
    ) -> None:
        self._file_backend = FileBackend(manager)
        self._qdrant_url = qdrant_url
        self._qdrant_api_key = qdrant_api_key
        self._embedding_model = embedding_model
        self._prefer_grpc = prefer_grpc
        self._in_memory = in_memory
        self._client = None
        self._embedder = None
        self._initialized_collections: set = set()

    def _get_client(self):
        """Lazy-init Qdrant client."""
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError:
                raise RuntimeError(
                    "qdrant-client is required for QdrantBackend. "
                    "Install with: pip install companest-orchestrator[qdrant]"
                )
            if self._in_memory:
                self._client = QdrantClient(location=":memory:")
            else:
                self._client = QdrantClient(
                    url=self._qdrant_url,
                    api_key=self._qdrant_api_key,
                    prefer_grpc=self._prefer_grpc,
                )
        return self._client

    def _get_embedder(self):
        """Lazy-init embedding model via fastembed."""
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise RuntimeError(
                    "fastembed is required for QdrantBackend embeddings. "
                    "Install with: pip install fastembed"
                )
            self._embedder = TextEmbedding(model_name=self._embedding_model)
        return self._embedder

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a list of texts."""
        embedder = self._get_embedder()
        return list(embedder.embed(texts))

    def _collection_name(self, team_id: str) -> str:
        """Sanitized Qdrant collection name."""
        safe_id = team_id.replace("/", "_").replace("\\", "_")
        return f"{_COLLECTION_PREFIX}{safe_id}"

    def _ensure_collection(self, team_id: str) -> None:
        """Create collection if it doesn't exist yet."""
        name = self._collection_name(team_id)
        if name in self._initialized_collections:
            return

        client = self._get_client()
        from qdrant_client.models import Distance, VectorParams

        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=_VECTOR_SIZE,
                    distance=Distance.COSINE,
                ),
            )
            logger.info("Created Qdrant collection: %s", name)

        self._initialized_collections.add(name)

    def _entry_to_text(self, key: str, meta: dict) -> str:
        """Build a searchable text representation of a memory entry."""
        parts = [key]
        if meta.get("summary"):
            parts.append(meta["summary"])
        if meta.get("tags"):
            parts.append(" ".join(meta["tags"]))
        return " | ".join(parts)

    def _upsert_entry(self, team_id: str, key: str) -> None:
        """Index or re-index a single entry in Qdrant."""
        self._ensure_collection(team_id)
        client = self._get_client()
        name = self._collection_name(team_id)

        index = self._file_backend.get_index(team_id)
        meta = index.get(key, {})
        text = self._entry_to_text(key, meta)
        vectors = self._embed([text])

        from qdrant_client.models import PointStruct
        point_id = self._key_to_point_id(key)
        client.upsert(
            collection_name=name,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vectors[0],
                    payload={"key": key, "team_id": team_id, **meta},
                )
            ],
        )

    def _delete_entry(self, team_id: str, key: str) -> None:
        """Remove a single entry from Qdrant."""
        name = self._collection_name(team_id)
        if name not in self._initialized_collections:
            return
        client = self._get_client()
        from qdrant_client.models import PointIdsList
        client.delete(
            collection_name=name,
            points_selector=PointIdsList(points=[self._key_to_point_id(key)]),
        )

    @staticmethod
    def _key_to_point_id(key: str) -> str:
        """Deterministic point ID from memory key."""
        import hashlib
        import uuid
        return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))

    #  Capability flags 

    @property
    def supports_semantic_search(self) -> bool:
        return True

    @property
    def supports_snapshot(self) -> bool:
        return True

    @property
    def supports_native_compaction(self) -> bool:
        return False

    #  Required operations (delegate to FileBackend, sync index) 

    def read(self, team_id: str, key: str) -> Any:
        return self._file_backend.read(team_id, key)

    def write(self, team_id: str, key: str, data: Any) -> None:
        self._file_backend.write(team_id, key, data)
        try:
            self._upsert_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to index %s/%s in Qdrant: %s", team_id, key, e)

    def append(self, team_id: str, key: str, entry: Any) -> None:
        self._file_backend.append(team_id, key, entry)
        try:
            self._upsert_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to index %s/%s in Qdrant: %s", team_id, key, e)

    def list_keys(self, team_id: str) -> List[str]:
        return self._file_backend.list_keys(team_id)

    def delete(self, team_id: str, key: str) -> None:
        self._file_backend.delete(team_id, key)
        try:
            self._delete_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to remove %s/%s from Qdrant: %s", team_id, key, e)

    def get_index(self, team_id: str) -> Dict[str, dict]:
        return self._file_backend.get_index(team_id)

    def update_meta(self, team_id: str, key: str, **fields) -> None:
        self._file_backend.update_meta(team_id, key, **fields)
        try:
            self._upsert_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to re-index %s/%s in Qdrant: %s", team_id, key, e)

    def archive(self, team_id: str, key: str) -> None:
        self._file_backend.archive(team_id, key)
        try:
            self._delete_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to remove archived %s/%s from Qdrant: %s", team_id, key, e)

    def search_archive(
        self, team_id: str, query: str, limit: int = 10,
    ) -> List[dict]:
        return self._file_backend.search_archive(team_id, query, limit)

    def read_overview(self, team_id: str) -> str:
        return self._file_backend.read_overview(team_id)

    def get_all_stats(self) -> Dict[str, Any]:
        return self._file_backend.get_all_stats()

    #  Snapshot (delegate to FileBackend) 

    def export_snapshot(self, team_id: str) -> Dict[str, Any]:
        return self._file_backend.export_snapshot(team_id)

    def restore_snapshot(self, team_id: str, snapshot: Dict[str, Any]) -> None:
        self._file_backend.restore_snapshot(team_id, snapshot)
        # Re-index all restored entries
        try:
            self._ensure_collection(team_id)
            index = self._file_backend.get_index(team_id)
            for key in index:
                self._upsert_entry(team_id, key)
            logger.info(
                "Re-indexed %d entries for team %s after snapshot restore",
                len(index), team_id,
            )
        except Exception as e:
            logger.warning("Failed to re-index team %s after restore: %s", team_id, e)

    #  Semantic search 

    def semantic_search(
        self,
        team_id: str,
        query: str,
        *,
        limit: int = 20,
        include_archive: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Vector similarity search over team memory entries.

        Returns list of dicts with 'key', 'score', 'source', and metadata.
        """
        self._ensure_collection(team_id)
        client = self._get_client()
        name = self._collection_name(team_id)

        vectors = self._embed([query])

        hits = client.search(
            collection_name=name,
            query_vector=vectors[0],
            limit=limit,
        )

        results: List[Dict[str, Any]] = []
        for hit in hits:
            payload = hit.payload or {}
            results.append({
                "key": payload.get("key", ""),
                "score": hit.score,
                "source": "active",
                "summary": payload.get("summary", ""),
                "tags": payload.get("tags", []),
                "importance": payload.get("importance", 0.0),
            })

        return results

    #  Helpers 

    @property
    def manager(self) -> MemoryManager:
        """Access the underlying MemoryManager (e.g. for prompt building)."""
        return self._file_backend.manager

    def reindex_team(self, team_id: str) -> int:
        """
        Full reindex of a team's memory into Qdrant.

        Useful after bulk import or if the index is out of sync.
        Returns the number of entries indexed.
        """
        self._ensure_collection(team_id)
        index = self._file_backend.get_index(team_id)
        if not index:
            return 0

        texts = []
        keys = []
        for key, meta in index.items():
            texts.append(self._entry_to_text(key, meta))
            keys.append(key)

        vectors = self._embed(texts)

        from qdrant_client.models import PointStruct
        points = [
            PointStruct(
                id=self._key_to_point_id(key),
                vector=vec,
                payload={"key": key, "team_id": team_id, **index[key]},
            )
            for key, vec in zip(keys, vectors)
        ]

        client = self._get_client()
        name = self._collection_name(team_id)
        # Batch upsert (Qdrant handles batching internally)
        client.upsert(collection_name=name, points=points)

        logger.info("Reindexed %d entries for team %s", len(points), team_id)
        return len(points)
