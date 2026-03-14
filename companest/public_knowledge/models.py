"""
Pydantic models for the Public Knowledge subsystem.

Matches contracts/public_knowledge_doc.schema.json.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class PublicKnowledgeDoc(BaseModel):
    """A structured document in the shared public knowledge pool."""

    doc_id: str = Field(..., description="Unique document identifier (UUID)")
    source_type: Literal["news", "research", "filing", "social", "prediction_market"] = Field(
        ..., description="Category of the information source"
    )
    title: str = Field(..., min_length=1, max_length=500, description="Document title")
    summary: str = Field(
        ..., min_length=1, max_length=5000, description="Document summary or abstract"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Free-text tags. Use prefixes: topic:, market:, entity:, event:, source:",
    )
    source_url: str = Field(..., description="Original source URL")
    published_at: datetime = Field(
        ..., description="When the source was originally published (ISO8601)"
    )
    fresh_until: datetime = Field(
        ..., description="Document is considered fresh/relevant until this time (ISO8601)"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence score of the information (0=unreliable, 1=highly reliable)",
    )
    visibility: Literal["public", "company_private"] = Field(
        ..., description="public = shared across all companies; company_private = restricted"
    )


class PublicKnowledgeIndexEntry(BaseModel):
    """Index entry: same fields as PublicKnowledgeDoc minus summary."""

    doc_id: str
    source_type: Literal["news", "research", "filing", "social", "prediction_market"]
    title: str = Field(..., min_length=1, max_length=500)
    tags: List[str] = Field(default_factory=list)
    source_url: str
    published_at: datetime
    fresh_until: datetime
    confidence: float = Field(..., ge=0.0, le=1.0)
    visibility: Literal["public", "company_private"]

    @classmethod
    def from_doc(cls, doc: PublicKnowledgeDoc) -> "PublicKnowledgeIndexEntry":
        """Create an index entry from a full document (drops summary)."""
        return cls(
            doc_id=doc.doc_id,
            source_type=doc.source_type,
            title=doc.title,
            tags=doc.tags,
            source_url=doc.source_url,
            published_at=doc.published_at,
            fresh_until=doc.fresh_until,
            confidence=doc.confidence,
            visibility=doc.visibility,
        )


class SearchQuery(BaseModel):
    """Query parameters for searching the public knowledge index."""

    tags: Optional[List[str]] = Field(default=None, description="Filter by tags (any match)")
    source_type: Optional[str] = Field(default=None, description="Filter by exact source_type")
    fresh_only: bool = Field(default=False, description="Only return docs where fresh_until > now")
    keyword: Optional[str] = Field(default=None, description="Case-insensitive title search")
    limit: int = Field(default=20, ge=1, le=200, description="Max results to return")
