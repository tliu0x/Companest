"""
Companest Public Knowledge Subsystem

Shared public knowledge layer backed by S3. Provides structured document
storage, indexing, search, and publishing for cross-company knowledge sharing.

- PublicKnowledgeDoc: Pydantic model matching public_knowledge_doc.schema.json
- PublicKnowledgeS3Store: S3-backed document storage with lazy boto3 init
- IndexStore: JSONL index management with expiry eviction
- PublicKnowledgePublisher: Coordinated publish/unpublish of docs + index
- search: In-memory filtering over the index

Feature flag: ENABLE_PUBLIC_KNOWLEDGE_V1
"""

from .models import PublicKnowledgeDoc, PublicKnowledgeIndexEntry, SearchQuery
from .s3_store import PublicKnowledgeS3Store
from .index_store import IndexStore
from .search import search
from .publisher import PublicKnowledgePublisher

__all__ = [
    "PublicKnowledgeDoc",
    "PublicKnowledgeIndexEntry",
    "SearchQuery",
    "PublicKnowledgeS3Store",
    "IndexStore",
    "search",
    "PublicKnowledgePublisher",
]
