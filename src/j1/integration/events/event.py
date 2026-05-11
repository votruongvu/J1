from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

# Default `source` for events emitted from the framework. Adapters can
# override per-publication when they want a finer-grained source URI.
DEFAULT_EVENT_SOURCE = "j1/knowledge-base"

# Supported event types — the catalog the spec calls out. Adapters may
# emit additional types; subscribers filter on whatever string they like.
EVENT_DOCUMENT_UPLOADED = "document.uploaded"
EVENT_DOCUMENT_PARSING_STARTED = "document.parsing_started"
EVENT_DOCUMENT_PARSING_COMPLETED = "document.parsing_completed"
EVENT_DOCUMENT_INGESTION_STARTED = "document.ingestion_started"
EVENT_DOCUMENT_INGESTION_COMPLETED = "document.ingestion_completed"
EVENT_DOCUMENT_INGESTION_FAILED = "document.ingestion_failed"
EVENT_DOCUMENT_INDEXING_STARTED = "document.indexing_started"
EVENT_DOCUMENT_INDEXING_COMPLETED = "document.indexing_completed"
EVENT_KNOWLEDGE_UPDATED = "knowledge.updated"
EVENT_QUERY_COMPLETED = "query.completed"
EVENT_ANSWER_GENERATED = "answer.generated"
EVENT_CITATION_VALIDATION_FAILED = "citation.validation_failed"


KB_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_DOCUMENT_UPLOADED,
        EVENT_DOCUMENT_PARSING_STARTED,
        EVENT_DOCUMENT_PARSING_COMPLETED,
        EVENT_DOCUMENT_INGESTION_STARTED,
        EVENT_DOCUMENT_INGESTION_COMPLETED,
        EVENT_DOCUMENT_INGESTION_FAILED,
        EVENT_DOCUMENT_INDEXING_STARTED,
        EVENT_DOCUMENT_INDEXING_COMPLETED,
        EVENT_KNOWLEDGE_UPDATED,
        EVENT_QUERY_COMPLETED,
        EVENT_ANSWER_GENERATED,
        EVENT_CITATION_VALIDATION_FAILED,
    }
)


_EMPTY_DATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ApplicationEvent:
    """Transport-neutral event emitted from inside the framework.

 Carries no HTTP / CloudEvents / webhook concerns. Adapters map this to
 their wire format (REST, MCP, Webhook, etc.). Frozen + value-typed so
 it's safe to hand to multiple subscribers concurrently.

 `actor` and `auth_type` are populated from the inbound `SecurityContext`
 when the event is triggered by an authenticated request. Events emitted
 by background workers / Temporal activities leave them None.
 """

    id: str
    type: str
    occurred_at: datetime
    source: str = DEFAULT_EVENT_SOURCE
    subject: str | None = None
    tenant_id: str | None = None
    correlation_id: str | None = None
    actor: str | None = None
    auth_type: str | None = None
    data: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_DATA)
