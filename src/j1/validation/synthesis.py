"""LLM answer synthesis for the manual-test-query path.

The Validation tab's manual query console runs one tester question
against a single ingestion run's retrieved chunks. Historically the
"answer" field was a deterministic snippet bundle (top-3 chunks'
titles + previews) — useful for retrieval debugging but not for
validating the end-user-facing RAG answer. This module adds a thin
wrapper that calls the configured `TextLLMClient` to synthesize a
final answer grounded in the retrieved chunks.

Scope:
 * Only invoked from `IngestionValidationService.run_manual_test_query`
   when `request.synthesize` is True AND a synthesizer is wired.
 * Batch validation runs and the public `/answer` endpoint do NOT
   flow through here — those paths must stay deterministic so check
   results are reproducible across re-runs.

Failure mode: any error from the LLM client (timeout, transport,
provider 5xx) is captured and returned as `(None, error_message)` so
the service can record `llm.error` on the response instead of
500ing. The deterministic checks still run against the existing
retrieval `answer` regardless of synthesis outcome.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from j1.llm.clients import TextLLMClient
from j1.validation.dtos import EvidenceBlockDTO

_log = logging.getLogger("j1.validation.synthesis")


# Per-block character cap inside the prompt. The evidence builder
# upstream already truncates each block to a sensible size; this is a
# second defensive cap so an unusually long block can't blow the
# context budget alone.
_MAX_CHUNK_CHARS = 1500

# Hard cap on the joined context. Keeps the prompt comfortably under
# the smallest practical context window (8K-tokens ≈ 6K chars after
# system+question overhead).
_MAX_CONTEXT_CHARS = 6000

# Completion budget. Short by design — the synthesized answer should
# be a 1-3 sentence operator-readable summary, not a re-derivation
# of the evidence.
_MAX_OUTPUT_TOKENS = 256

# Grounding prompt. Strict on hallucination ("ONLY the context") but
# explicit that direct inference is allowed AND that direct quotes
# from the evidence are allowed even when the question's wording
# differs. Earlier wording caused the model to abstain even when the
# evidence literally contained the answer because the question
# phrased it differently — the failure mode the operator reported
# ("citations include chunk + compiled.text, but synthesis still says
# 'Not in the retrieved evidence'"). The "[N]" citation convention
# matches the numbered evidence blocks the prompt builder emits so
# the user can verify which block supported each claim.
_SYSTEM_PROMPT = (
    "You answer questions about an ingested document using ONLY the "
    "evidence blocks provided below. Be concise (1-3 sentences). "
    "Reference the evidence blocks like [1] or [2] when supporting a "
    "claim. Direct inference from the evidence is allowed — answer "
    "when the evidence clearly implies the answer, even if the exact "
    "phrasing differs. When the evidence contains the answer verbatim "
    "but uses different wording than the question, prefer quoting "
    "the evidence over abstaining. Whitespace differences (extra "
    "spaces, line breaks, hyphenation) between the question and the "
    "evidence do not count as missing evidence. Only reply \"Not in "
    "the retrieved evidence.\" when the evidence neither states nor "
    "implies the answer. Do not invent facts or reference outside "
    "knowledge."
)


# Canonical fallback strings the system prompt instructs the LLM to
# emit. Mirrored in ``judge._FALLBACK_PHRASES`` so groundedness
# whitelisting and synthesis fallback detection use the same
# vocabulary. Substring match, whitespace-normalised, case-
# insensitive.
_FALLBACK_MARKERS: tuple[str, ...] = (
    "not in the retrieved evidence",
    "not enough information",
    "no relevant information",
    "the evidence does not",
    "the retrieved evidence does not",
)


def _is_fallback_text(text: str) -> bool:
    """Detect whether the LLM emitted one of the canonical
    insufficient-evidence phrases. Used so the service can populate
    ``fallback_reason="llm_abstained"`` in the debug payload even
    when the answer round-trip succeeded."""
    if not text:
        return False
    norm = " ".join(text.lower().split())
    return any(m in norm for m in _FALLBACK_MARKERS)


@dataclass(frozen=True)
class SynthesisResult:
    """Outcome of one `AnswerSynthesizer.synthesize` call.

 `answer` is `None` when synthesis was not attempted (no evidence)
 or when it failed (`error` populated). Token counts are
 best-effort — providers that don't surface usage report zeros.
 """

    answer: str | None
    provider: str | None
    model: str | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None


class AnswerSynthesizer(Protocol):
    """Protocol the validation service depends on; one implementation
 ships (`DefaultAnswerSynthesizer`). Tests substitute a stub so
 the service can be exercised without a live LLM."""

    def synthesize(
        self,
        *,
        question: str,
        evidence: Sequence[EvidenceBlockDTO],
    ) -> SynthesisResult: ...


class DefaultAnswerSynthesizer:
    """LLM-backed synthesizer. Builds a context-grounded prompt from
 the supplied evidence blocks and calls `TextLLMClient.generate`.
 Any client exception is captured and surfaced via
 `SynthesisResult.error` — the caller decides whether to expose
 it to the FE.

 The synthesizer is intentionally dumb about evidence quality:
 it trusts the caller (the validation service) to have already
 loaded real chunk bodies, deduped, and budgeted. This module's
 only job is "string formatting + one LLM call"."""

    def __init__(self, text_client: TextLLMClient) -> None:
        self._client = text_client

    def synthesize(
        self,
        *,
        question: str,
        evidence: Sequence[EvidenceBlockDTO],
    ) -> SynthesisResult:
        provider = getattr(self._client, "provider", None)
        model = getattr(self._client, "model", None)

        if not evidence:
            return SynthesisResult(
                answer=None,
                provider=provider,
                model=model,
                latency_ms=None,
                prompt_tokens=None,
                completion_tokens=None,
                error="no_evidence",
            )

        prompt = _build_prompt(question, evidence)
        started = time.monotonic()
        try:
            text, usage = self._client.generate(
                prompt,
                system_prompt=_SYSTEM_PROMPT,
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                metadata={"caller": "validation.manual_query"},
            )
        except Exception as exc:  # noqa: BLE001 — bubble up as recorded trace, not 500
            _log.warning(
                "answer synthesis failed (provider=%s model=%s): %s",
                provider, model, exc,
            )
            return SynthesisResult(
                answer=None,
                provider=provider,
                model=model,
                latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=None,
                completion_tokens=None,
                error=f"{type(exc).__name__}: {exc}"[:512],
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        answer_text = text.strip() if isinstance(text, str) else None
        # Distinguish "LLM ran successfully" from "LLM abstained
        # using the canonical fallback phrase" so the validation
        # service's debug payload can classify it as
        # ``fallback_reason=llm_abstained`` instead of a silent
        # success. The answer text is still surfaced — the
        # operator may want to see it.
        fallback_marker = (
            "llm_abstained" if answer_text and _is_fallback_text(answer_text)
            else None
        )
        return SynthesisResult(
            answer=answer_text,
            provider=getattr(usage, "provider", provider),
            model=getattr(usage, "model", model),
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "input_tokens", None) or None,
            completion_tokens=getattr(usage, "output_tokens", None) or None,
            error=fallback_marker,
        )


def _build_prompt(
    question: str, evidence: Sequence[EvidenceBlockDTO],
) -> str:
    """Compose the user-message body. Numbered evidence blocks let
 the LLM reference passages via `[N]` in its answer; the FE
 renders the same numbering in the "Evidence Sent to LLM"
 panel so a tester can verify which block backed each claim."""
    parts: list[str] = []
    used = 0
    for idx, block in enumerate(evidence, start=1):
        text = (block.text or "").strip()
        if not text:
            continue
        if len(text) > _MAX_CHUNK_CHARS:
            text = text[:_MAX_CHUNK_CHARS].rstrip() + "…"

        # Header carries the artifact type + page info when present
        # so the model knows whether it's reading a chunk fragment
        # or a compiled-text window. Helps grounding decisions for
        # "where in the doc does X happen?" style questions.
        header_bits: list[str] = [f"[{idx}]"]
        if block.artifact_type:
            header_bits.append(f"({block.artifact_type})")
        if block.page_start is not None:
            if block.page_end is not None and block.page_end != block.page_start:
                header_bits.append(f"pages {block.page_start}-{block.page_end}")
            else:
                header_bits.append(f"page {block.page_start}")
        if block.section:
            header_bits.append(f"§ {block.section}")
        header = " ".join(header_bits)

        block_str = f"{header}\n{text}"
        if used + len(block_str) > _MAX_CONTEXT_CHARS:
            break
        parts.append(block_str)
        used += len(block_str)

    context_block = "\n\n".join(parts) if parts else "(no evidence retrieved)"
    return f"Question: {question}\n\nEvidence:\n{context_block}"


__all__ = [
    "AnswerSynthesizer",
    "DefaultAnswerSynthesizer",
    "SynthesisResult",
]
