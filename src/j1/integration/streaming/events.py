from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# Per-event-type names per the streaming spec — kept as plain strings so
# callers in any layer can compare without importing the constant module.
STREAM_EVENT_ANSWER_STARTED = "answer.started"
STREAM_EVENT_RETRIEVAL_STARTED = "retrieval.started"
STREAM_EVENT_RETRIEVAL_COMPLETED = "retrieval.completed"
STREAM_EVENT_GENERATION_DELTA = "generation.delta"
STREAM_EVENT_CITATION_ADDED = "citation.added"
STREAM_EVENT_ANSWER_COMPLETED = "answer.completed"
STREAM_EVENT_ANSWER_FAILED = "answer.failed"


ANSWER_STREAM_EVENT_TYPES: frozenset[str] = frozenset(
    {
        STREAM_EVENT_ANSWER_STARTED,
        STREAM_EVENT_RETRIEVAL_STARTED,
        STREAM_EVENT_RETRIEVAL_COMPLETED,
        STREAM_EVENT_GENERATION_DELTA,
        STREAM_EVENT_CITATION_ADDED,
        STREAM_EVENT_ANSWER_COMPLETED,
        STREAM_EVENT_ANSWER_FAILED,
    }
)


_EMPTY_DATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class AnswerStreamEvent:
    """One step in a per-request answer stream.

 Transport-neutral: contains no HTTP / SSE concerns. The REST adapter
 formats this into `text/event-stream`; future MCP / WebSocket
 adapters could format it into their own framing without touching
 this class.

 The `event` value is one of `STREAM_EVENT_*` constants for the
 framework's built-in lifecycle events; custom event types are
 permitted for extensions.
 """

    request_id: str
    event: str
    data: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_DATA)

    def to_payload(self) -> dict[str, Any]:
        """Plain dict suitable for JSON serialisation by adapters."""
        return {
            "requestId": self.request_id,
            "event": self.event,
            "data": dict(self.data),
        }
