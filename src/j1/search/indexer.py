"""Phase 8: legacy SQLite search index DELETED.

The `SqliteSearchIndexer` class is gone. Phase 6 migrated the BM25
auxiliary retrieval route to `LexicalEvidenceAdapter` over Postgres
FTS; Phase 7 retired the kind registration; Phase 8 deletes the
class entirely.

The `SearchHit` dataclass survives because the integration DTO
(`SearchService`) historically built one from SQLite results. It's
no longer constructed by any active path — kept here purely so any
legacy import from ``j1.search.indexer`` doesn't break with
``ImportError`` during a single dev cycle. Phase 9 deletes it too.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchHit:
    """Phase 8 trace-only shim. Constructed by the integration
    DTO's legacy ``_hit_to_dto`` helper, which is itself reachable
    only via the historical (now-empty) SearchService SQLite path.
    No active code path produces a ``SearchHit``."""

    artifact_id: str
    artifact_type: str
    title: str
    source_document_id: str | None
    source_location: str | None
    confidence: float
    review_status: str
    checksum: str
    created_at: str
    byte_size: int
    extracted_text: str
    score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    chunk_id: str | None = None


__all__ = ["SearchHit"]
