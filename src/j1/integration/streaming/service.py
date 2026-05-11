import logging
from typing import Any

from j1.integration.dto import AnswerDTO, AnswerRequestDTO
from j1.integration.security import SecurityContext
from j1.integration.streaming.events import (
    STREAM_EVENT_ANSWER_COMPLETED,
    STREAM_EVENT_ANSWER_FAILED,
    STREAM_EVENT_ANSWER_STARTED,
    STREAM_EVENT_CITATION_ADDED,
    STREAM_EVENT_GENERATION_DELTA,
    STREAM_EVENT_RETRIEVAL_COMPLETED,
    STREAM_EVENT_RETRIEVAL_STARTED,
    AnswerStreamEvent,
)
from j1.integration.streaming.handler import AnswerStreamHandler
from j1.projects.context import ProjectContext

_log = logging.getLogger(__name__)

# Word count per `generation.delta` chunk. Small enough that a few
# hundred-word answer produces multiple deltas (so receivers can render
# incrementally), large enough that a tiny answer doesn't blow up into
# many trivial events.
DEFAULT_DELTA_WORDS_PER_CHUNK = 8

# Spec-shaped, public-safe error payload for stream failures. Returned
# verbatim — never enriched with raw exception text.
SAFE_GENERATION_FAILED_PAYLOAD: dict[str, Any] = {
    "code": "ANSWER_GENERATION_FAILED",
    "message": "Answer generation failed.",
    "retryable": True,
}


class AnswerStreamingService:
    """Drives the answer-stream lifecycle.

 Wraps a vendor-neutral `AnswerPort` (today the same `AnswerService`
 used by non-streaming `/answer`) and emits the seven framework
 `STREAM_EVENT_*` types in spec order. The wrapped port runs
 unchanged — this service neither rewrites retrieval nor touches the
 `HybridQueryEngine`.

 **Streaming model.** The current `AnswerPort` is synchronous /
 single-shot; once a real token-streaming `ModelProvider` is wired
 in, this service is the only place that needs to change. Today the
 answer text is synthesised first, then chunked into N
 `generation.delta` events word-by-word so consumers can still render
 incrementally. The API surface and event types are the same in both
 worlds.

 **Security.** The service never inspects credentials or applies
 scope rules — that's the adapter's job. It does carry the resolved
 `SecurityContext` so receivers (e.g. event-bus webhooks) get the
 same `kbactor` attribution as non-streaming answers.

 **Error handling.** Any exception during retrieval/generation is
 masked with `SAFE_GENERATION_FAILED_PAYLOAD` and emitted as
 `answer.failed`. The full exception is logged with the request_id
 for operator triage but never surfaces in the stream.
 """

    def __init__(
        self,
        answer_port,
        *,
        words_per_delta: int = DEFAULT_DELTA_WORDS_PER_CHUNK,
    ) -> None:
        self._answer_port = answer_port
        self._words_per_delta = max(1, int(words_per_delta))

    def stream(
        self,
        ctx: ProjectContext,
        request: AnswerRequestDTO,
        *,
        request_id: str,
        handler: AnswerStreamHandler,
        security: SecurityContext | None = None,
    ) -> AnswerDTO | None:
        """Run the full lifecycle, pushing events to `handler`.

 Returns the final `AnswerDTO` on success, `None` on failure
 (the failure is surfaced via the `answer.failed` event).
 """
        actor = (
            security.subject
            if security is not None and not security.is_anonymous
            else None
        )

        self._emit(handler, request_id, STREAM_EVENT_ANSWER_STARTED, {
            "question": request.question,
            "mode": request.mode,
            "actor": actor,
        })
        self._emit(handler, request_id, STREAM_EVENT_RETRIEVAL_STARTED, {
            "mode": request.mode,
        })

        try:
            dto: AnswerDTO = self._answer_port.answer(ctx, request)
        except Exception as exc:
            # Mask. Log with the correlation id — operators can grep.
            _log.exception(
                "answer streaming failed for request_id=%s tenant=%s actor=%s: %s",
                request_id, ctx.tenant_id, actor, exc.__class__.__name__,
            )
            self._emit(handler, request_id, STREAM_EVENT_ANSWER_FAILED,
                       dict(SAFE_GENERATION_FAILED_PAYLOAD))
            return None

        self._emit(handler, request_id, STREAM_EVENT_RETRIEVAL_COMPLETED, {
            "sourceCount": len(dto.sources),
            "modeUsed": dto.mode_used,
            "relatedArtifactCount": len(dto.related_artifacts),
        })

        for chunk in _chunk_text(dto.answer, self._words_per_delta):
            self._emit(handler, request_id, STREAM_EVENT_GENERATION_DELTA, {
                "text": chunk,
            })

        # The current architecture surfaces citations only after retrieval
        # / synthesis. Emit one citation.added per source so receivers can
        # bind UI rows incrementally.
        for citation in dto.sources:
            self._emit(handler, request_id, STREAM_EVENT_CITATION_ADDED, {
                "artifactId": citation.artifact_id,
                "artifactType": citation.artifact_type,
                "sourceDocumentId": citation.source_document_id,
                "sourceLocation": citation.source_location,
            })

        self._emit(handler, request_id, STREAM_EVENT_ANSWER_COMPLETED, {
            "modeUsed": dto.mode_used,
            "citationCount": len(dto.sources),
            "confidence": dto.confidence,
            "confidenceLevel": dto.confidence_level,
            "reviewRequired": dto.review_required,
            "warnings": list(dto.warnings),
            "warningCategories": list(dto.warning_categories),
        })
        return dto

    @staticmethod
    def _emit(
        handler: AnswerStreamHandler,
        request_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        try:
            handler.handle(AnswerStreamEvent(
                request_id=request_id, event=event_type, data=data,
            ))
        except Exception:
            # Per the AnswerStreamHandler contract, handlers should not
            # raise. If one does, drop the event rather than abort the
            # whole run — partial streams are recoverable, aborted
            # responses are not.
            _log.exception(
                "stream handler failed for request_id=%s event=%s",
                request_id, event_type,
            )


def _chunk_text(text: str, words_per_chunk: int) -> list[str]:
    """Split `text` into space-joined word chunks of `words_per_chunk`.

 Empty / whitespace-only text yields an empty list (no deltas
 emitted) — receivers shouldn't see a hollow `generation.delta`.
 Trailing whitespace is preserved within chunks via `split`'s
 default behaviour.
 """
    words = text.split()
    if not words:
        return []
    return [
        " ".join(words[i:i + words_per_chunk])
        for i in range(0, len(words), words_per_chunk)
    ]
