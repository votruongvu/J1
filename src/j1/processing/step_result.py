"""Step-level result records used by the workflow to express what
actually happened at each pipeline stage.

These complement `ArtifactProcessingResult` (which is per-adapter) by
adding workflow-only context: requiredness, source of the
inclusion/skip decision, recorded skip / failure reason, and timing.

Kept deliberately small — no document content, no LLM payloads, no
artifact bytes. Safe to attach to audit events, status responses, and
Temporal search-attribute / memo summaries (subject to the same
no-PII rules)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from j1.processing.status import StepSource, StepStatus

__all__ = ["StepError", "StepResult"]


# Cap user-facing error messages so a verbose vendor stack trace
# doesn't blow up audit storage / Temporal payload limits. The
# truncation marker is added so downstream readers know there's more.
_MAX_ERROR_MESSAGE_BYTES = 1024
_TRUNCATION_MARKER = " …[truncated]"


@dataclass(frozen=True)
class StepError:
    """Compact error summary attached to a failed `StepResult`.

    `type` is the exception class name (`exc.__class__.__name__`), not
    a fully-qualified import path — operators don't need the module.
    `message` is the human-readable string from the exception, capped
    at 1 KB. `retryable` mirrors Temporal's `non_retryable` flag (a
    `True` here means "the framework considers this transient and
    expects retry to help"); used by status reporting and tests, not
    consulted by the SDK."""

    type: str
    message: str
    retryable: bool = False

    @classmethod
    def from_exception(
        cls, exc: BaseException, *, retryable: bool = False,
    ) -> "StepError":
        text = str(exc)
        if len(text.encode("utf-8")) > _MAX_ERROR_MESSAGE_BYTES:
            text = text[: _MAX_ERROR_MESSAGE_BYTES - len(_TRUNCATION_MARKER)]
            text += _TRUNCATION_MARKER
        return cls(
            type=exc.__class__.__name__,
            message=text,
            retryable=retryable,
        )


@dataclass(frozen=True)
class StepResult:
    """Workflow's record of a single planned stage's outcome.

    Aggregated into the per-document and per-job final summary so
    operators / status endpoints / audit logs can answer:

      * Which stages ran?
      * Which were skipped, and why?
      * Which failed, with what error?
      * How long did each take?
      * Was the stage required by plan / caller / policy?

    The `started_at`/`completed_at` pair is for stage timing only;
    individual activity attempts (with retries) are tracked by
    Temporal itself.

    `metadata` is intentionally a small free-form dict for adapter-
    supplied operational hints (e.g. `parser_method`, `model_role`,
    `artifact_count`). Do NOT put document text, prompts, or LLM
    responses here — this struct is logged and stored verbatim."""

    step: str
    status: StepStatus
    required: bool
    source: StepSource
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    reason: str | None = None
    error: StepError | None = None
    artifact_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """True when no further work is expected on this step."""
        return self.status in (
            StepStatus.COMPLETED,
            StepStatus.SKIPPED,
            StepStatus.FAILED,
        )

    def is_failure(self) -> bool:
        return self.status == StepStatus.FAILED
