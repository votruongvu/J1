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
from j1.validation.dtos import RetrievedChunkRefDTO

_log = logging.getLogger("j1.validation.synthesis")


# Per-chunk character cap. Local LLMs (8B-class) choke on long
# contexts; trading per-chunk fidelity for total-context room keeps
# the call within the configured context window.
_MAX_CHUNK_CHARS = 1500

# Hard cap on the joined context. Keeps the prompt comfortably under
# the smallest practical context window (8K-tokens ≈ 6K chars after
# system+question overhead).
_MAX_CONTEXT_CHARS = 6000

# Completion budget. Short by design — the synthesized answer should
# be a 1-3 sentence operator-readable summary, not a re-derivation
# of the evidence.
_MAX_OUTPUT_TOKENS = 256

_SYSTEM_PROMPT = (
    "You answer questions about an ingested document using ONLY the "
    "context provided below. Be concise (1-3 sentences). If the "
    "answer is not present in the context, reply exactly: "
    '"Not present in the retrieved evidence." Do not invent facts, '
    "do not reference outside knowledge, do not list sources."
)


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
        chunks: Sequence[RetrievedChunkRefDTO],
    ) -> SynthesisResult: ...


class DefaultAnswerSynthesizer:
    """LLM-backed synthesizer. Builds a context-grounded prompt from
 the retrieved chunks and calls `TextLLMClient.generate`. Any
 client exception is captured and surfaced via `SynthesisResult.error`
 — the caller decides whether to expose it to the FE."""

    def __init__(self, text_client: TextLLMClient) -> None:
        self._client = text_client

    def synthesize(
        self,
        *,
        question: str,
        chunks: Sequence[RetrievedChunkRefDTO],
    ) -> SynthesisResult:
        provider = getattr(self._client, "provider", None)
        model = getattr(self._client, "model", None)

        if not chunks:
            return SynthesisResult(
                answer=None,
                provider=provider,
                model=model,
                latency_ms=None,
                prompt_tokens=None,
                completion_tokens=None,
                error="no_evidence",
            )

        prompt = _build_prompt(question, chunks)
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
        return SynthesisResult(
            answer=text.strip() if isinstance(text, str) else None,
            provider=getattr(usage, "provider", provider),
            model=getattr(usage, "model", model),
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "input_tokens", None) or None,
            completion_tokens=getattr(usage, "output_tokens", None) or None,
            error=None,
        )


def _build_prompt(
    question: str, chunks: Sequence[RetrievedChunkRefDTO],
) -> str:
    """Compose the user-message body. Numbered context blocks let the
 LLM reference passages internally without us needing to parse
 citations out of free-text — we display the evidence separately
 in the UI."""
    parts: list[str] = []
    used = 0
    for idx, chunk in enumerate(chunks, start=1):
        preview = (chunk.preview or "").strip()
        if not preview:
            continue
        if len(preview) > _MAX_CHUNK_CHARS:
            preview = preview[:_MAX_CHUNK_CHARS] + "…"
        # +budget for the surrounding "[N] …\n\n" framing
        if used + len(preview) > _MAX_CONTEXT_CHARS:
            break
        parts.append(f"[{idx}] {preview}")
        used += len(preview)

    context_block = "\n\n".join(parts) if parts else "(no evidence retrieved)"
    return f"Question: {question}\n\nContext:\n{context_block}"


__all__ = [
    "AnswerSynthesizer",
    "DefaultAnswerSynthesizer",
    "SynthesisResult",
]
