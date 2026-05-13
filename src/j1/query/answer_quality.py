"""AnswerQualityGate — the final-status decision after synthesis.

The composite gate replaces the legacy ``aggregate_status`` rule
that let long refusals pass. Each rule here is explicit:

  1. answer_nonempty — the LLM returned non-whitespace text.
  2. answer_not_refusal — when the plan demands a substantive
     answer (``fail_on_refusal=True``), the text must not match
     any refusal pattern.
  3. answer_shape_matches — for table / list shapes, the answer
     contains the structural markers (``|`` for tables, list
     bullets, etc.).
  4. required_fields_covered — the answer mentions each requested
     field at least once (per-row matching for table shapes).
  5. citations_subset — every used citation index resolves to a
     selected block; cited ⊆ selected.

If ALL required gates pass, the orchestrator returns
``QueryFinalStatus.PASSED``. Otherwise, ``FAILED`` with the first
failure's reason surfaced on the response.

No length heuristic. No "long answer means substantive" shortcut.
"""

from __future__ import annotations

import re
from enum import StrEnum

from j1.query.answer_synthesizer import SynthesisOutput
from j1.query.query_plan import (
    AnswerShape,
    EvidenceBlock,
    GateResult,
    QueryPlan,
)


# Stable gate-result names; consumers / UI match these strings.
GATE_ANSWER_NONEMPTY = "answer_nonempty"
GATE_ANSWER_NOT_REFUSAL = "answer_not_refusal"
GATE_ANSWER_SHAPE = "answer_shape"
GATE_REQUIRED_FIELDS = "required_fields_covered"
GATE_CITATIONS_SUBSET = "citations_subset"


class QueryFinalStatus(StrEnum):
    """Stable status strings. ``PASSED`` means every required gate
    passed; ``FAILED`` is any required failure; ``EVIDENCE_INSUFFICIENT``
    / ``RETRIEVAL_INSUFFICIENT`` come from upstream gates and are
    passed through unchanged."""

    PASSED = "passed"
    FAILED = "failed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    RETRIEVAL_INSUFFICIENT = "retrieval_insufficient"


# ---- Refusal patterns ----------------------------------------
#
# Generic across documents. The patterns intentionally do NOT use
# a length heuristic — a 600-char "I'm sorry, but the retrieved
# evidence doesn't contain..." answer must still fail.

_REFUSAL_PATTERNS = (
    re.compile(r"\bnot\s+(?:in|present\s+in|found\s+in)\s+(?:the\s+)?"
               r"(?:retrieved\s+)?evidence\b", re.IGNORECASE),
    re.compile(r"\bi\s+(?:can(?:not|'t)|am\s+unable\s+to)\s+answer\b",
               re.IGNORECASE),
    re.compile(r"\bno\s+(?:relevant\s+)?(?:information|evidence)\s+"
               r"(?:was\s+|is\s+)?(?:found|available)\b",
               re.IGNORECASE),
    re.compile(r"\binsufficient\s+(?:context|evidence|information)\b",
               re.IGNORECASE),
    re.compile(r"\bi\s+don'?t\s+have\s+(?:enough\s+|the\s+)?"
               r"(?:information|context|evidence)\b", re.IGNORECASE),
    re.compile(r"\bunable\s+to\s+(?:determine|answer|provide)\b",
               re.IGNORECASE),
)


def _looks_like_refusal(answer: str) -> bool:
    if not answer or not answer.strip():
        return True
    for pat in _REFUSAL_PATTERNS:
        if pat.search(answer):
            return True
    return False


# ---- Shape checks --------------------------------------------


def _shape_matches(answer: str, shape: AnswerShape) -> bool:
    if not answer:
        return False
    a = answer.strip()
    if shape in {
        AnswerShape.STAGE_BY_STAGE_TABLE,
        AnswerShape.SIDE_BY_SIDE_TABLE,
        AnswerShape.DELIVERABLE_MATRIX,
    }:
        # Markdown table markers — at least one line with ``|`` AND
        # a separator line ``|---|``.
        has_pipes = "|" in a
        has_separator = bool(re.search(r"\|\s*-{3,}\s*\|", a))
        return has_pipes and has_separator
    if shape in {AnswerShape.REQUIREMENT_LIST, AnswerShape.RISK_LIST}:
        # Numbered or bulleted list — a line starting with ``- `` or
        # ``\d+.``.
        return bool(re.search(r"(?m)^(?:\s*[-*]|\s*\d+\.)\s+", a))
    if shape == AnswerShape.BULLET_LIST:
        return bool(re.search(r"(?m)^(?:\s*[-*])\s+", a))
    if shape == AnswerShape.SHORT_FACT:
        # A short fact is a single declarative sentence — accept
        # answers ≤ 2 sentences.
        sentences = [s for s in re.split(r"[.!?]+\s", a) if s.strip()]
        return 1 <= len(sentences) <= 2
    # PARAGRAPH and unmatched shapes: any non-empty text passes.
    return bool(a)


# ---- Required-fields check -----------------------------------


_FIELD_STOPWORDS: frozenset[str] = frozenset({
    # Generic English filler that the classifier sometimes pulls
    # into a requested field (e.g. "modules involved", "scope of",
    # "the deliverables"). Stripping these means a field like
    # "modules involved" passes when the answer talks about
    # "modules" without using the literal phrase.
    "a", "an", "the",
    "of", "for", "to", "from", "in", "on", "at", "by", "with",
    "and", "or", "vs", "vs.",
    "is", "are", "was", "were", "be", "being", "been",
    "involved", "involve", "involves",
    "associated", "related", "applicable",
    "any", "all", "each", "every", "some",
})


def _field_tokens(field: str) -> list[str]:
    """Split a requested-field label into content tokens. Empty
    list means "no signal" — the gate treats that field as
    trivially satisfied rather than spuriously failing."""
    tokens: list[str] = []
    for raw in re.split(r"[\s\-_/]+", (field or "").lower()):
        t = re.sub(r"[^a-z0-9]+", "", raw)
        if not t or t in _FIELD_STOPWORDS or len(t) <= 1:
            continue
        tokens.append(t)
    return tokens


def _fields_covered(
    answer: str, fields: tuple[str, ...],
) -> tuple[bool, list[str]]:
    """A requested field is covered when each of its content tokens
    appears in the answer (case-insensitive). Stopwords / filler
    are stripped first so that e.g. ``"modules involved"`` is
    satisfied by an answer that says "the modules are A, B, C".
    Returns ``(passed, missing_fields)``."""
    if not fields:
        return True, []
    a = (answer or "").lower()
    missing: list[str] = []
    for f in fields:
        tokens = _field_tokens(f)
        if not tokens:
            # Field reduced to nothing after stopword removal — no
            # content to check, treat as satisfied so we don't fail
            # on classifier noise like "the" or "of".
            continue
        if not all(t in a for t in tokens):
            missing.append(f)
    return (not missing, missing)


# ---- Gate ----------------------------------------------------


class AnswerQualityGate:
    """Composite gate. ``check(plan, output, cited, selected)``
    returns ``(gate_results, final_status)``."""

    def check(
        self,
        plan: QueryPlan,
        output: SynthesisOutput,
        *,
        cited: tuple[EvidenceBlock, ...],
        selected: tuple[EvidenceBlock, ...],
    ) -> tuple[tuple[GateResult, ...], QueryFinalStatus]:
        results: list[GateResult] = []

        # --- nonempty ----------------------------------------
        nonempty = bool(output.answer and output.answer.strip())
        results.append(GateResult(
            name=GATE_ANSWER_NONEMPTY,
            passed=nonempty,
            severity="required",
            reason=(None if nonempty else
                    "synthesizer returned empty text"),
        ))

        # --- not_refusal -------------------------------------
        if plan.quality.fail_on_refusal and nonempty:
            refusal = _looks_like_refusal(output.answer)
            results.append(GateResult(
                name=GATE_ANSWER_NOT_REFUSAL,
                passed=not refusal,
                severity="required",
                reason=(
                    None if not refusal else
                    "answer matches a refusal / no-answer pattern"
                ),
            ))
        else:
            results.append(GateResult(
                name=GATE_ANSWER_NOT_REFUSAL,
                passed=True, severity="advisory",
                detail={
                    "skipped": (
                        not nonempty or
                        not plan.quality.fail_on_refusal
                    ),
                },
            ))

        # --- answer_shape ------------------------------------
        if nonempty:
            shape_ok = _shape_matches(
                output.answer, plan.quality.answer_shape,
            )
            results.append(GateResult(
                name=GATE_ANSWER_SHAPE,
                passed=shape_ok,
                severity="required",
                reason=(
                    None if shape_ok else
                    f"answer does not match required shape "
                    f"{plan.quality.answer_shape.value}"
                ),
            ))
        else:
            results.append(GateResult(
                name=GATE_ANSWER_SHAPE,
                passed=False,
                severity="required",
                reason="no answer to shape-check",
            ))

        # --- required_fields ---------------------------------
        if plan.quality.required_fields:
            covered, missing = _fields_covered(
                output.answer, plan.quality.required_fields,
            )
            results.append(GateResult(
                name=GATE_REQUIRED_FIELDS,
                passed=covered,
                severity="required",
                reason=(
                    None if covered else
                    f"answer missing requested fields: {missing}"
                ),
                detail={"missing": missing},
            ))
        else:
            results.append(GateResult(
                name=GATE_REQUIRED_FIELDS,
                passed=True, severity="advisory",
            ))

        # --- citations_subset --------------------------------
        selected_keys = {
            (b.candidate.artifact_id, b.candidate.chunk_id)
            for b in selected
        }
        rogue = [
            (b.candidate.artifact_id, b.candidate.chunk_id)
            for b in cited
            if (b.candidate.artifact_id, b.candidate.chunk_id)
            not in selected_keys
        ]
        subset_ok = not rogue
        results.append(GateResult(
            name=GATE_CITATIONS_SUBSET,
            passed=subset_ok,
            severity="required",
            reason=(
                None if subset_ok else
                f"cited blocks not in selected pack: {rogue}"
            ),
            detail={"cited_count": len(cited),
                    "selected_count": len(selected)},
        ))

        # Composite verdict — any required failure → FAILED.
        for r in results:
            if r.severity == "required" and not r.passed:
                return tuple(results), QueryFinalStatus.FAILED
        return tuple(results), QueryFinalStatus.PASSED


__all__ = [
    "AnswerQualityGate",
    "GATE_ANSWER_NONEMPTY",
    "GATE_ANSWER_NOT_REFUSAL",
    "GATE_ANSWER_SHAPE",
    "GATE_CITATIONS_SUBSET",
    "GATE_REQUIRED_FIELDS",
    "QueryFinalStatus",
]
