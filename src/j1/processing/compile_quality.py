"""Compile-result quality evaluation + retry-decision contract.

A `CompileQualityEvaluator` looks at an `ArtifactProcessingResult`
that compile already returned and answers two questions:

 1. **Quality bucket** — is the output `good`, `low`, or `failed`?
 2. **Retry-eligible?** — should the workflow rerun compile with a
 higher `CompileMode`?

It MUST be deterministic, cheap, and pure — no I/O, no LLM calls,
no MinerU/RAGAnything reinvocation. The whole point of this module
is to gate the expensive retry decision on signals already present
in the result.

Retry rules (the bar must be CLEAR — the prompt is explicit:
"do not retry just because a better result might be possible"):

 * `chunks_count == 0` — compile returned no chunks. Almost
 certainly bad output; retry with a deeper mode.
 * `extracted_text_chars` below `min_text_chars` — the result
 has too little text to be useful. Likely a parser regression
 or a parse_method mismatch (e.g. `txt` mode on a scanned PDF).
 * Compile activity returned `status=failed` with a message that
 looks recoverable (parser empty, OCR-needed signal, transient
 layout error). Hard parser crashes (file-not-found, license
 violation) don't retry.
 * Plan required OCR but compile resolved to `parse_method != ocr`
 AND the result is empty or chars-below-threshold. Triggers a
 deeper retry that will lock OCR on.

The evaluator does NOT retry on ambiguous signals (e.g. low
confidence, unhandled capabilities) — those become warnings on
the final result but don't burn another compile attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from j1.processing.results import ArtifactProcessingResult, ResultStatus

# Quality bucket vocabulary. Stringly typed because it travels via
# `ArtifactProcessingResult.metadata["final_compile_quality"]` and
# the FE consumes it through the artifact-listing surface.
QUALITY_GOOD = "good"
QUALITY_LOW = "low"
QUALITY_FAILED = "failed"

# Retry-reason vocabulary. Each value is a stable short string the
# audit log + UI both read. Adding a new reason should be a coordinated
# release — operators may build dashboards on these.
RETRY_REASON_ZERO_CHUNKS = "zero_chunks"
RETRY_REASON_LOW_TEXT = "low_text_chars"
RETRY_REASON_OCR_LIKELY_NEEDED = "ocr_likely_needed"
RETRY_REASON_RECOVERABLE_FAILURE = "recoverable_failure"


# Default thresholds. Operators tune via
# `J1_COMPILE_RETRY_MIN_TEXT_CHARS` /
# `J1_COMPILE_RETRY_MIN_CHUNKS` (see `compile_retry_settings.py`).
DEFAULT_MIN_TEXT_CHARS = 200
DEFAULT_MIN_CHUNKS = 1


@dataclass(frozen=True)
class QualityVerdict:
    """The evaluator's output. `quality` always populated;
 `retry_reason` non-None iff the workflow should burn another
 compile attempt. `signals` is the operator-readable counts the
 evaluator looked at (chunk_count, extracted_text_chars) so
 audit logs / UI render concrete numbers, not just buckets."""

    quality: str  # QUALITY_GOOD | QUALITY_LOW | QUALITY_FAILED
    retry_reason: str | None
    signals: dict[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def should_retry(self) -> bool:
        return self.retry_reason is not None


# Recoverable failure-message substrings. When the compile activity
# returns `status=failed` with a message containing any of these
# substrings, the retry layer treats the failure as retryable with
# a higher mode. Hard failures (file-not-found, license errors,
# vendor crashes) never appear in this list and so never retry.
_RECOVERABLE_FAILURE_PATTERNS: tuple[str, ...] = (
    "no content",
    "empty result",
    "ocr",
    "scanned",
    "layout output does not match",
    "parsing failed",
)


def evaluate_compile_quality(
    result: ArtifactProcessingResult,
    *,
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    min_chunks: int = DEFAULT_MIN_CHUNKS,
    plan_required_ocr: bool = False,
    parse_method_used: str | None = None,
) -> QualityVerdict:
    """Bucket the compile result and decide whether to retry.

 Args:
 `result`: the just-returned compile output.
 `min_text_chars`: threshold below which the chars-extracted
 signal counts as `low_text_chars` retry-trigger. Defaults
 sized for "a single short page or two of useful text" —
 operators with tighter / looser quality bars override via
 `CompileRetrySettings`.
 `min_chunks`: threshold below which the chunk-count signal
 counts as `zero_chunks` retry-trigger. The default = 1
 means "at least one chunk must exist"; operators in
 chunk-skeptical environments can set higher.
 `plan_required_ocr`: True when the AssessmentPlan required
 OCR. Combined with a non-OCR `parse_method_used` and a
 thin result, triggers `ocr_likely_needed` retry.
 `parse_method_used`: the parse_method that produced
 `result`. Used only for the OCR-mismatch detection above.

 Returns a `QualityVerdict` — the workflow inspects
 `should_retry` and `quality` to decide what to do next.
 """
    chunks_count = _chunks_count(result)
    extracted_text_chars = _extracted_text_chars(result)
    notes: list[str] = []

    # ---- Hard failure first -----------------------------------
    # Compile activity returned status=failed. Distinguish recoverable
    # failure (retry might help) from terminal failure (don't retry).
    if result.status == ResultStatus.FAILED:
        message = (result.error or result.message or "").lower()
        if any(p in message for p in _RECOVERABLE_FAILURE_PATTERNS):
            return QualityVerdict(
                quality=QUALITY_FAILED,
                retry_reason=RETRY_REASON_RECOVERABLE_FAILURE,
                signals={
                    "chunks_count": chunks_count,
                    "extracted_text_chars": extracted_text_chars,
                    "failure_message": result.error or result.message,
                },
                notes=tuple(notes),
            )
        return QualityVerdict(
            quality=QUALITY_FAILED,
            retry_reason=None,
            signals={
                "chunks_count": chunks_count,
                "extracted_text_chars": extracted_text_chars,
                "failure_message": result.error or result.message,
            },
            notes=("failure does not match recoverable patterns",),
        )

    # ---- Zero-chunks ------------------------------------------
    if chunks_count < min_chunks:
        notes.append(
            f"chunks_count={chunks_count} below threshold {min_chunks}"
        )
        return QualityVerdict(
            quality=QUALITY_LOW,
            retry_reason=RETRY_REASON_ZERO_CHUNKS,
            signals={
                "chunks_count": chunks_count,
                "extracted_text_chars": extracted_text_chars,
                "min_chunks_threshold": min_chunks,
            },
            notes=tuple(notes),
        )

    # ---- Low-text ---------------------------------------------
    if extracted_text_chars is not None and extracted_text_chars < min_text_chars:
        # Special case: plan required OCR but parse_method didn't
        # fire OCR. The retry should escalate to a mode that DOES
        # use OCR, not just rerun the same config.
        if plan_required_ocr and (parse_method_used or "").lower() != "ocr":
            return QualityVerdict(
                quality=QUALITY_LOW,
                retry_reason=RETRY_REASON_OCR_LIKELY_NEEDED,
                signals={
                    "chunks_count": chunks_count,
                    "extracted_text_chars": extracted_text_chars,
                    "min_text_chars_threshold": min_text_chars,
                    "parse_method_used": parse_method_used,
                },
                notes=(
                    "plan required OCR but parse_method didn't fire it; "
                    "retry with deeper mode to enable OCR",
                ),
            )
        return QualityVerdict(
            quality=QUALITY_LOW,
            retry_reason=RETRY_REASON_LOW_TEXT,
            signals={
                "chunks_count": chunks_count,
                "extracted_text_chars": extracted_text_chars,
                "min_text_chars_threshold": min_text_chars,
            },
            notes=tuple(notes),
        )

    return QualityVerdict(
        quality=QUALITY_GOOD,
        retry_reason=None,
        signals={
            "chunks_count": chunks_count,
            "extracted_text_chars": extracted_text_chars,
        },
        notes=tuple(notes),
    )


def _chunks_count(result: ArtifactProcessingResult) -> int:
    """Count chunk artifacts by walking `result.drafts` for
 `kind == "chunk"`. Falls back to `metadata["chunks_count"]` /
 `metadata["text_block_count"]` when the producer surfaced an
 explicit count instead of individual drafts (some adapters
 emit one composite chunk artifact + a count in metadata)."""
    from j1.processing.results import ARTIFACT_KIND_CHUNK
    drafts = getattr(result, "drafts", None) or ()
    chunk_drafts = [
        d for d in drafts
        if getattr(d, "kind", None) == ARTIFACT_KIND_CHUNK
    ]
    if chunk_drafts:
        return len(chunk_drafts)
    metadata = getattr(result, "metadata", None) or {}
    for key in ("chunks_count", "chunk_count", "text_block_count"):
        v = metadata.get(key)
        if isinstance(v, int):
            return v
    return 0


def _extracted_text_chars(
    result: ArtifactProcessingResult,
) -> int | None:
    """Read total text chars from the compile result's metadata
 (`total_text_chars` is what the bridge's manifest builder
 surfaces). Returns None when the producer didn't emit the
 signal — the evaluator treats None as "unknown" and skips the
 chars-below-threshold rule rather than retrying defensively."""
    metadata = getattr(result, "metadata", None) or {}
    for key in ("total_text_chars", "extracted_text_chars"):
        v = metadata.get(key)
        if isinstance(v, int):
            return v
    return None


__all__ = [
    "DEFAULT_MIN_CHUNKS",
    "DEFAULT_MIN_TEXT_CHARS",
    "QUALITY_FAILED",
    "QUALITY_GOOD",
    "QUALITY_LOW",
    "QualityVerdict",
    "RETRY_REASON_LOW_TEXT",
    "RETRY_REASON_OCR_LIKELY_NEEDED",
    "RETRY_REASON_RECOVERABLE_FAILURE",
    "RETRY_REASON_ZERO_CHUNKS",
    "evaluate_compile_quality",
]
