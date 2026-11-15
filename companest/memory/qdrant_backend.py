import logging
import threading
from typing import Any, Dict, List, Optional

from ..utils import synchronized
from .backend import MemoryBackend, FileBackend
from .manager import MemoryManager

logger = logging.getLogger(__name__)

_COLLECTION_PREFIX = "companest_memory_"
_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_VECTOR_SIZE = 384  # bge-small-en-v1.5 output dimension


class QdrantBackend(MemoryBackend):
    """Hybrid memory backend: FileBackend for storage + Qdrant for semantic search."""

    def __init__(
        self,
        manager: MemoryManager,
        qdrant_url: str = "http://localhost:6333",
        qdrant_api_key: Optional[str] = None,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        prefer_grpc: bool = False,
        in_memory: bool = False,
    ) -> None:
        self._io_lock = threading.RLock()
        self._file_backend = FileBackend(manager)
        self._qdrant_url = qdrant_url
        self._qdrant_api_key = qdrant_api_key
        self._embedding_model = embedding_model
        self._prefer_grpc = prefer_grpc
        self._in_memory = in_memory
        self._client = None
        self._embedder = None
        self._initialized_collections: set = set()

    @synchronized
    def _get_client(self):
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

    @synchronized
    def _get_embedder(self):
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

    @synchronized
    def _embed(self, texts: List[str]) -> List[List[float]]:
        return list(self._get_embedder().embed(texts))

    @synchronized
    def _collection_name(self, team_id: str) -> str:
        safe_id = team_id.replace("/", "_").replace("\\", "_")
        return f"{_COLLECTION_PREFIX}{safe_id}"

    @synchronized
    def _ensure_collection(self, team_id: str) -> None:
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

    @synchronized
    def _entry_to_text(self, key: str, meta: dict) -> str:
        """Build a searchable text representation of a memory entry."""
        parts = [key]
        if meta.get("summary"):
            parts.append(meta["summary"])
        if meta.get("tags"):
            parts.append(" ".join(meta["tags"]))
        return " | ".join(parts)

    @synchronized
    def _upsert_entry(self, team_id: str, key: str) -> None:
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

    @synchronized
    def _delete_entry(self, team_id: str, key: str) -> None:
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

    @property
    @synchronized
    def supports_semantic_search(self) -> bool:
        return True

    @synchronized
    def _index_safe(self, team_id: str, key: str, verb: str) -> None:
        try:
            self._upsert_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to %s %s/%s in Qdrant: %s", verb, team_id, key, e)

    @synchronized
    def _unindex_safe(self, team_id: str, key: str, verb: str) -> None:
        try:
            self._delete_entry(team_id, key)
        except Exception as e:
            logger.warning("Failed to %s %s/%s from Qdrant: %s", verb, team_id, key, e)

    @synchronized
    def read(self, team_id: str, key: str) -> Any:
        return self._file_backend.read(team_id, key)

    @synchronized
    def write(self, team_id: str, key: str, data: Any) -> None:
        self._file_backend.write(team_id, key, data)
        self._index_safe(team_id, key, "index")

    @synchronized
    def append(self, team_id: str, key: str, entry: Any) -> None:
        self._file_backend.append(team_id, key, entry)
        self._index_safe(team_id, key, "index")

    @synchronized
    def list_keys(self, team_id: str) -> List[str]:
        return self._file_backend.list_keys(team_id)

    @synchronized
    def delete(self, team_id: str, key: str) -> None:
        self._file_backend.delete(team_id, key)
        self._unindex_safe(team_id, key, "remove")

    @synchronized
    def get_index(self, team_id: str) -> Dict[str, dict]:
        return self._file_backend.get_index(team_id)

    @synchronized
    def update_meta(self, team_id: str, key: str, **fields) -> None:
        self._file_backend.update_meta(team_id, key, **fields)
        self._index_safe(team_id, key, "re-index")

    @synchronized
    def archive(self, team_id: str, key: str) -> None:
        self._file_backend.archive(team_id, key)
        self._unindex_safe(team_id, key, "remove archived")

    @synchronized
    def search_archive(
        self, team_id: str, query: str, limit: int = 10,
    ) -> List[dict]:
        return self._file_backend.search_archive(team_id, query, limit)

    @synchronized
    def read_overview(self, team_id: str) -> str:
        return self._file_backend.read_overview(team_id)

    @synchronized
    def get_all_stats(self) -> Dict[str, Any]:
        return self._file_backend.get_all_stats()

    @synchronized
    def semantic_search(
        self,
        team_id: str,
        query: str,
        *,
        limit: int = 20,
        include_archive: bool = True,
    ) -> List[Dict[str, Any]]:
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
