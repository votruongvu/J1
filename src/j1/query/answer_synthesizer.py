"""AnswerSynthesizer — produces the answer text from a selected
evidence pack.

The synthesizer is the only orchestrator component that calls an
LLM. The contract is narrow:

  * It reads ONLY the selected evidence blocks. It does not see the
    raw candidate list, it does not call retrieval, it does not
    talk to the database.
  * It is gated upstream by the sufficiency check — when the gate
    failed, the orchestrator skips synthesis entirely.
  * It uses a SHAPE-SPECIFIC prompt for each ``AnswerShape``. The
    failed-question quality failure was driven in part by a single
    generic prompt that asked for prose. Shape-specific prompts
    force the LLM into the structure the quality gate checks
    against.
  * It declares which evidence blocks it actually used. The
    ``SynthesisOutput`` carries the subset; the citation binder
    consumes that subset (NOT the broader pack).

The LLM is injected as a plain ``Callable[[SynthesisRequest], str]``
so tests substitute deterministic stubs and production wires the
``LLMProviderRegistry.text()`` client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from j1.query.domain_profile import DomainProfile, GENERIC_PROFILE
from j1.query.field_tokens import field_tokens
from j1.query.query_plan import (
    AnswerShape,
    EvidenceBlock,
    Intent,
    QueryPlan,
    SynthesisMode,
)

# Per-block truncation in the user prompt. Chunked evidence is
# capped to a moderate length (the LLM doesn't need 4KB of prose to
# decide a fact). The native-answer block — RAGAnything's already-
# synthesized prose — is given a much larger window because the
# local synthesizer's job is to PASS that content through, not to
# re-summarise it. Truncating the native answer to 1200 chars
# regularly cut off the very sentence that named the requested field.
_BLOCK_CHAR_CAP_DEFAULT = 1200
_BLOCK_CHAR_CAP_NATIVE_ANSWER = 6000

_RAGANYTHING_NATIVE_ANSWER_KIND = "raganything.native_answer"


# ---- Synthesis request / output -------------------------------


@dataclass(frozen=True)
class SynthesisRequest:
    """What the LLM callable sees. The system prompt is shape-
    specific; the user prompt carries the question + serialised
    evidence blocks. The synthesizer assembles both."""

    plan: QueryPlan
    blocks: tuple[EvidenceBlock, ...]
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class SynthesisOutput:
    """Output of one synthesis call. ``used_block_indices`` is the
    subset of ``input_blocks`` the LLM actually drew from — when
    the LLM signals "I used block #2 and #4", those indices land
    here. The citation binder consumes this to produce final
    citations.

    Empty ``used_block_indices`` is valid — it means the
    synthesizer couldn't ground the answer and the quality gate
    will flag a refusal."""

    answer: str
    used_block_indices: tuple[int, ...]
    raw_llm_output: str = ""


# ---- LLM callable type ----------------------------------------


LLMCallable = Callable[[SynthesisRequest], str]
"""Sync function: build a string answer from a request. The string
SHOULD include block-citation tags ``[#N]`` that map to evidence
block indices; the synthesizer extracts them into
``used_block_indices``. Stubs in tests return canned strings."""


# ---- Synthesizer ---------------------------------------------


class AnswerSynthesizer:
    """Pure component except for the injected LLM callable. The
    callable is what makes the synthesizer non-deterministic in
    production; everything else is a pure function of the inputs."""

    def __init__(
        self,
        *,
        llm: LLMCallable,
    ) -> None:
        self._llm = llm

    def synthesize(
        self,
        plan: QueryPlan,
        blocks: tuple[EvidenceBlock, ...],
        *,
        profile: DomainProfile | None = None,
    ) -> SynthesisOutput:
        profile = profile or GENERIC_PROFILE
        system_prompt = _system_prompt(plan, profile)
        user_prompt = _user_prompt(plan, blocks)
        request = SynthesisRequest(
            plan=plan,
            blocks=blocks,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        try:
            raw = self._llm(request)
        except Exception as exc:  # noqa: BLE001 — LLM failure surfaces
            return SynthesisOutput(
                answer="",
                used_block_indices=(),
                raw_llm_output=f"<error: {type(exc).__name__}: {exc}>",
            )
        # The LLM convention: include ``[#N]`` tags pointing to
        # the 1-indexed block number from the user_prompt. The
        # synthesizer extracts those and treats them as the cited-
        # set.
        used = _extract_used_indices(raw, len(blocks))
        output = SynthesisOutput(
            answer=raw.strip(),
            used_block_indices=used,
            raw_llm_output=raw,
        )
        # Native-answer pass-through fallback. If the local
        # synthesizer dropped requested-field content that the
        # RAGAnything native answer DOES contain, swap in the
        # native answer. Without this, the synthesizer's tendency
        # to over-summarise can omit content RAGAnything already
        # surfaced — operators see "answer missing requested
        # fields" even though the upstream answer had them.
        return _apply_native_answer_fallback(plan, output, blocks)


# ---- Prompt assembly -----------------------------------------


def _system_prompt(plan: QueryPlan, profile: DomainProfile) -> str:
    """Shape-specific system prompt. The prompt forces the LLM into
    the structure the quality gate verifies, AND tells it to cite
    block numbers (which the binder then resolves to artifact_id)."""
    shape = plan.quality.answer_shape
    base = (
        "You are an extraction assistant. Answer the user's question "
        "USING ONLY the provided evidence blocks. "
        "Cite the blocks you use by their number tag in square "
        "brackets — for example: [#1], [#2]. "
    )
    if plan.synthesis_mode == SynthesisMode.EXTRACT_ONLY:
        base += (
            "Quote the source text verbatim when possible; do not "
            "paraphrase. "
        )
    elif plan.synthesis_mode == SynthesisMode.PROJECT_STRUCTURED:
        base += (
            "Do not infer missing values. If a row or column has no "
            "supporting evidence, leave it blank or write "
            "\"not in retrieved evidence\". "
        )
    # Shape-specific tail.
    if shape == AnswerShape.STAGE_BY_STAGE_TABLE:
        fields = ", ".join(plan.requested_fields) or "Description"
        base += (
            "Return a Markdown table with columns: Stage, "
            f"{fields}, Citation. One row per stage. "
            "Stages: " + ", ".join(plan.anchors) + ". "
        )
    elif shape == AnswerShape.SIDE_BY_SIDE_TABLE:
        base += (
            "Return a Markdown table comparing the entities the "
            "question names, with one column per entity. "
        )
    elif shape == AnswerShape.REQUIREMENT_LIST:
        base += (
            "Return a numbered list. Each item: ID, Requirement text, "
            "Source citation. "
        )
    elif shape == AnswerShape.RISK_LIST:
        base += (
            "Return a numbered list. Each item: Risk, Likelihood / "
            "Impact (if stated), Mitigation, Source citation. "
        )
    elif shape == AnswerShape.DELIVERABLE_MATRIX:
        base += (
            "Return a Markdown matrix. Rows: deliverables. Columns: "
            "stage / party / phase as appropriate. Cite each cell. "
        )
    elif shape == AnswerShape.SHORT_FACT:
        base += (
            "Reply with a single sentence stating the fact, "
            "followed by the citation tag. "
        )
    elif shape == AnswerShape.BULLET_LIST:
        base += "Reply with a Markdown bullet list. "
    else:
        base += "Reply in concise prose, one paragraph maximum. "

    # Domain hint appended last — domain packs can append style
    # guidance without rewriting the system prompt.
    hint = profile.prompt_hints.get(plan.intent, "")
    if hint:
        base += hint
    return base


def _user_prompt(
    plan: QueryPlan, blocks: tuple[EvidenceBlock, ...],
) -> str:
    """Question + numbered evidence blocks. Tag format is ``[#N]``
    so the LLM and the binder agree on the same syntax."""
    lines: list[str] = []
    lines.append(f"Question: {plan.normalized_question}")
    lines.append("")
    if plan.requested_fields:
        lines.append(
            "Requested fields: " + ", ".join(plan.requested_fields)
        )
        lines.append(
            "Your answer MUST address every Requested Field that is "
            "supported by the evidence below. Do not omit a field "
            "when an evidence block mentions it."
        )
    if plan.anchors:
        lines.append("Anchors: " + ", ".join(plan.anchors))
    lines.append("")
    lines.append("Evidence blocks:")
    for i, block in enumerate(blocks, start=1):
        kind = block.candidate.artifact_kind
        loc = block.candidate.chunk_id or block.candidate.artifact_id
        lines.append(
            f"[#{i}] (kind={kind}, source={loc}, "
            f"group={block.group or 'general'})"
        )
        cap = (
            _BLOCK_CHAR_CAP_NATIVE_ANSWER
            if kind == _RAGANYTHING_NATIVE_ANSWER_KIND
            else _BLOCK_CHAR_CAP_DEFAULT
        )
        lines.append(block.body.strip()[:cap])
        lines.append("")
    return "\n".join(lines).strip()


# ---- Native-answer fallback ----------------------------------


def _apply_native_answer_fallback(
    plan: QueryPlan,
    output: SynthesisOutput,
    blocks: tuple[EvidenceBlock, ...],
) -> SynthesisOutput:
    """When the local synthesizer's answer is missing a requested-
    field token that a ``raganything.native_answer`` block already
    contains, swap in the native answer. Returns ``output``
    unchanged when the swap doesn't apply.

    Why a swap and not an inline merge: the native answer is itself
    a fully-formed RAGAnything answer (LightRAG ran retrieval +
    LLM synthesis upstream). Re-summarising it loses content. When
    the local synthesizer demonstrably dropped content the native
    answer had, the safest correction is to use the native answer
    verbatim and cite the native-answer block."""
    if not plan.requested_fields:
        return output
    answer_lower = (output.answer or "").lower()
    # Which fields did the synthesizer drop?
    missing: list[str] = []
    for f in plan.requested_fields:
        tokens = field_tokens(f)
        if not tokens:
            continue
        if not all(t in answer_lower for t in tokens):
            missing.append(f)
    if not missing:
        return output
    # Find a native-answer block whose body covers every missing
    # field's tokens. The block is allowed to be substantially
    # longer than the synthesizer's answer — that's the point.
    fallback_idx: int | None = None
    fallback_body: str = ""
    for i, block in enumerate(blocks):
        if block.candidate.artifact_kind != _RAGANYTHING_NATIVE_ANSWER_KIND:
            continue
        body = (block.body or "").strip()
        if not body:
            continue
        body_l = body.lower()
        if all(
            all(t in body_l for t in field_tokens(f))
            for f in missing
        ):
            fallback_idx = i
            fallback_body = body
            break
    if fallback_idx is None:
        return output
    # Swap. The new ``used_block_indices`` cites ONLY the native-
    # answer block — citation binder + answer-quality gate work on
    # the new answer text alone.
    note = (
        "<native_answer_fallback: synthesizer dropped fields "
        f"{missing!r}; using raganything.native_answer block "
        f"#{fallback_idx + 1}>"
    )
    return SynthesisOutput(
        answer=fallback_body,
        used_block_indices=(fallback_idx,),
        raw_llm_output=(output.raw_llm_output or "") + "\n" + note,
    )


def _extract_used_indices(
    raw: str, block_count: int,
) -> tuple[int, ...]:
    """Pull ``[#N]`` tags out of the LLM response, dedupe, sort.
    Indices outside the valid block range are silently ignored —
    we never trust the LLM to cite blocks it wasn't given."""
    import re
    indices: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"\[#(\d+)\]", raw or ""):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        # 1-indexed → 0-indexed; reject out-of-range.
        if 1 <= idx <= block_count and idx not in seen:
            seen.add(idx)
            indices.append(idx - 1)
    return tuple(indices)


__all__ = [
    "AnswerSynthesizer",
    "LLMCallable",
    "SynthesisOutput",
    "SynthesisRequest",
]
