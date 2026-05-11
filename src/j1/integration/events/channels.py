"""Logical event-channel definitions and event-type → channel mapping.

These are **logical** channels — the broker-neutral grouping consumers
subscribe to ("give me everything that happens to documents"). Physical
broker topics / queues / streams are an infrastructure concern; an
adapter is free to map `kb.documents` to a Kafka topic, RabbitMQ
exchange, SQS queue URL, etc.

The mapping is defined here so it stays consistent across publishers,
the AsyncAPI spec, and consumers; the framework's existing event types
(in `j1.integration.events.event`) remain the single source of truth
for event names.
"""

from j1.integration.events.event import (
    EVENT_ANSWER_GENERATED,
    EVENT_CITATION_VALIDATION_FAILED,
    EVENT_DOCUMENT_INDEXING_COMPLETED,
    EVENT_DOCUMENT_INDEXING_STARTED,
    EVENT_DOCUMENT_INGESTION_COMPLETED,
    EVENT_DOCUMENT_INGESTION_FAILED,
    EVENT_DOCUMENT_INGESTION_STARTED,
    EVENT_DOCUMENT_PARSING_COMPLETED,
    EVENT_DOCUMENT_PARSING_STARTED,
    EVENT_DOCUMENT_UPLOADED,
    EVENT_KNOWLEDGE_UPDATED,
    EVENT_QUERY_COMPLETED,
)

CHANNEL_DOCUMENTS = "kb.documents"
CHANNEL_INGESTION = "kb.ingestion"
CHANNEL_INDEXING = "kb.indexing"
CHANNEL_QUERY = "kb.query"
CHANNEL_ANSWER = "kb.answer"
CHANNEL_CITATION = "kb.citation"
CHANNEL_AUDIT = "kb.audit"

LOGICAL_CHANNELS: tuple[str, ...] = (
    CHANNEL_DOCUMENTS,
    CHANNEL_INGESTION,
    CHANNEL_INDEXING,
    CHANNEL_QUERY,
    CHANNEL_ANSWER,
    CHANNEL_CITATION,
    CHANNEL_AUDIT,
)


# Single source of truth for "which logical channel does this event
# type belong to?". Used by the publisher to route, and by the
# AsyncAPI spec generator (or hand-maintained docs) to group messages.
EVENT_TYPE_TO_CHANNEL: dict[str, str] = {
    EVENT_DOCUMENT_UPLOADED: CHANNEL_DOCUMENTS,
    EVENT_DOCUMENT_PARSING_STARTED: CHANNEL_INGESTION,
    EVENT_DOCUMENT_PARSING_COMPLETED: CHANNEL_INGESTION,
    EVENT_DOCUMENT_INGESTION_STARTED: CHANNEL_INGESTION,
    EVENT_DOCUMENT_INGESTION_COMPLETED: CHANNEL_INGESTION,
    EVENT_DOCUMENT_INGESTION_FAILED: CHANNEL_INGESTION,
    EVENT_DOCUMENT_INDEXING_STARTED: CHANNEL_INDEXING,
    EVENT_DOCUMENT_INDEXING_COMPLETED: CHANNEL_INDEXING,
    EVENT_KNOWLEDGE_UPDATED: CHANNEL_INDEXING,
    EVENT_QUERY_COMPLETED: CHANNEL_QUERY,
    EVENT_ANSWER_GENERATED: CHANNEL_ANSWER,
    EVENT_CITATION_VALIDATION_FAILED: CHANNEL_CITATION,
}


def channel_for(event_type: str) -> str:
    """Return the logical channel for `event_type`, or `kb.audit` for unknowns.

 Routing every unknown / custom event to `kb.audit` is the safe
 default — operators get a single subscription that catches
 everything new without code changes.
 """
    return EVENT_TYPE_TO_CHANNEL.get(event_type, CHANNEL_AUDIT)
