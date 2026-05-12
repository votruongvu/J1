"""Generate validation test cases from an ingested document's chunks.

 ships a single concrete implementation: `DefaultTestCaseGenerator`.
It samples chunks from the run, asks the configured FAST/text LLM to
propose 1–2 retrieval-style questions per chunk, and emits a
`ValidationSetDTO` ready for the runner.

Design rules:

 * **Generated, not gold.** The output is labelled `source="generated"`
 and `status="draft"`. Tester edits / approval workflows live ; the field exists today so the wire shape doesn't churn.
 * **Deterministic fallback.** When no LLM is configured, or the LLM
 call fails for a given chunk, the generator falls back to a
 heuristic question authored from the chunk's first sentence. This
 keeps generation usable in test suites and on dev stacks without
 a FAST endpoint.
 * **Smoke first.** Every set carries at least one `priority="smoke"`
 case (a generic "what is this document about?" question). Smoke
 cases run first and gate the bulk.
 * **Source traceability.** Every emitted case carries the chunk_ids
 the generator consulted, so an operator can audit "where did this
 question come from?" without re-running anything.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.domains.models import DomainValidationGuidance
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation.dtos import (
    EvidenceBlockDTO,
    LLMTraceDTO,
    ValidationSetDTO,
    ValidationTestCaseDTO,
)

_log = logging.getLogger("j1.validation.generator")

# Generator version — bump when the case shape / sampling rules / prompt
# change in a way that should invalidate cached sets. Persisted on the
# `ValidationSetDTO`, used by callers as part of the idempotency key.
GENERATOR_VERSION = "v1"

# Hard caps. Match the plan: synchronous in-process execution
# only, total ≤ 50 cases. Sampling stays well under so the LLM round
# trip count is bounded.
_MAX_CHUNK_SAMPLES = 8
_MAX_QUESTIONS_PER_CHUNK = 2
_PREVIEW_MAX_CHARS = 240

# Hard cap on the heuristic fallback's question count. Used only
# when no LLM is wired AND no evidence blocks were supplied (the
# service typically supplies them); the heuristic produces one
# generic-form question per sampled chunk.
_HEURISTIC_QUESTIONS_PER_CHUNK = 1

# Default number of domain-driven negative checks emitted when the
# pack declares `negative_check_fields`. Each negative tests a
# different important field the document didn't cover; we cap to
# avoid pushing out the legitimate positive cases.
_DEFAULT_NEGATIVE_COUNT = 2

# Per-evidence-block character cap in the LLM prompt. Pairs with
# the upstream evidence builder (which already truncates each block);
# the second cap is a defensive bound so a long block can't crowd out
# the rest.
_MAX_EVIDENCE_BLOCK_CHARS = 1500

# Cumulative-evidence cap. Bigger than the synthesizer's because
# question generation needs a wider view of the document — the
# whole-document call is what stops the LLM from inventing topics.
_MAX_EVIDENCE_TOTAL_CHARS = 8000

# Max completion tokens for the whole-document generation call.
# Enough for 8 grounded cases × ~80 tokens each.
_MAX_OUTPUT_TOKENS = 1024

# Allowed question-type tags. Mirrors `ValidationQuestionType` —
# kept here so the JSON schema validator rejects out-of-vocabulary
# values from the LLM rather than letting them drift to the FE.
_ALLOWED_QUESTION_TYPES: tuple[str, ...] = (
    "fact_retrieval",
    "list_extraction",
    "table_extraction",
    "summary",
    "risk_extraction",
    "constraint_extraction",
    "reasoning_from_context",
    "domain_enrichment_check",
    "missing_information_check",
)

# Allowed validation scopes. Same rationale.
_ALLOWED_SCOPES: tuple[str, ...] = (
    "generic",
    "domain_evidence",
    "domain_enrichment",
    "negative_check",
)

# Whole-document structured-output schema. The LLM must emit cases
# with question, expected_answer, evidence quote, and a tag set;
# server-side validation drops any case whose quote doesn't actually
# appear in the supplied evidence (the anti-hallucination check).
_GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "expected_answer": {"type": "string"},
                    "question_type": {
                        "type": "string",
                        "enum": list(_ALLOWED_QUESTION_TYPES),
                    },
                    "validation_scope": {
                        "type": "string",
                        "enum": list(_ALLOWED_SCOPES),
                    },
                    "difficulty": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_artifact_id": {"type": "string"},
                                "artifact_type": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["source_artifact_id", "quote"],
                        },
                    },
                },
                "required": [
                    "question",
                    "expected_answer",
                    "question_type",
                    "validation_scope",
                    "evidence",
                ],
            },
        },
    },
    "required": ["test_cases"],
}


# Strict grounding prompt. Replaces the prior "given a document chunk,
# propose questions" wording that let the LLM drift to outside-world
# topics. Three explicit rules: only-evidence, copy-the-quote, use
# domain guidance as a lens not a fact source.
_SYSTEM_PROMPT = (
    "You generate validation questions for a RAG ingestion system. "
    "You must only use the provided evidence context as the source of "
    "truth. Do not use outside knowledge. Do not invent facts. "
    "Domain guidance, if provided, is only a rubric for choosing "
    "useful validation angles — it is not evidence and must never "
    "create unsupported expected answers. "
    "Every positive question must be answerable from the evidence, "
    "and every expected answer must be supported by a verbatim quote "
    "copied from the evidence context. If domain guidance flags a "
    "field that the evidence does not cover, generate a "
    "missing_information_check question with validation_scope = "
    '"negative_check" and an expected_answer that begins with '
    '"No." or "Not specified". Never invent expected answers from '
    "domain guidance. Return JSON only."
)


# ---- Public dataclass ---------------------------------------------


@dataclass(frozen=True)
class GenerationOptions:
    """Caller-tunable parameters. Kept as a tiny value object so the
 REST request → service → generator chain doesn't grow a six-arg
 function signature."""

    max_cases: int = 25
    citation_required: bool = False
    # Chunk-id force-include list. When non-empty, the generator
    # ensures these chunks show up in the sample (if they exist).
    # Useful for "regenerate but keep my last set's coverage."
    must_include_chunk_ids: tuple[str, ...] = ()
    # count of off-topic negative cases to include. The
    # generator picks them deterministically from `_NEGATIVE_PROMPTS`
    # so the same set is reproducible across regenerations. Zero
    # disables negatives entirely (e.g. for very small case budgets).
    negative_case_count: int = _DEFAULT_NEGATIVE_COUNT
    # per-modality cap on emitted cases. The generator
    # samples up to N table / N image / N graph cases (where N
    # below) to keep the total bounded. Zero disables a modality
    # entirely; high values are still subject to the global
    # `max_cases` ceiling.
    max_table_cases: int = 3
    max_image_cases: int = 3
    max_graph_cases: int = 3


# ---- Generator -----------------------------------------------------


class DefaultTestCaseGenerator:
    """'s single implementation.

 Holds the LLM client (text role; FAST is acceptable too — same
 `extract(prompt, schema)` surface). Generation is synchronous —
 the REST handler awaits one call, and the cap on chunk samples
 keeps the worst-case round trips bounded (8 chunks × 1 LLM call
 each = 8 calls, ~5–10 seconds total against a local LLM).
 """

    def __init__(
        self,
        *,
        text_client: Any | None = None,
        prompt: str | None = None,
    ) -> None:
        self._text_client = text_client
        # The whole-document system prompt is fixed (constant in the
        # module so tests can assert exact wording). The constructor
        # still accepts a `prompt` override for harness-level
        # experiments but the new flow ignores it for the system
        # message — see `_SYSTEM_PROMPT`. Legacy callers that passed
        # a custom prompt fall back to the heuristic path; this is
        # an intentional simplification.
        self._prompt = prompt

    def generate(
        self,
        *,
        run_id: str,
        document_ids: list[str],
        chunks: list[_ChunkRecord],
        options: GenerationOptions | None = None,
        actor: str | None = None,
        # optional modality artifacts. Each list is the
        # subset of the run's artifacts of the given kind. The
        # service builds these from the registry; the generator
        # treats empty lists as "no modality cases to emit."
        table_artifacts: list[ArtifactRecord] | None = None,
        visual_artifacts: list[ArtifactRecord] | None = None,
        graph_artifacts: list[ArtifactRecord] | None = None,
        # NEW: real evidence blocks (chunk bodies + compiled.text
        # windows) the service built via `build_evidence_blocks`.
        # When supplied, the LLM gets a single whole-document call
        # instead of N per-chunk calls — this is what stops the
        # generator from emitting off-topic questions.
        evidence_blocks: list[EvidenceBlockDTO] | None = None,
        # NEW: domain guidance loaded from `domain.yaml`'s
        # `validation:` block. Used purely as a testing-lens
        # rubric — never as factual evidence. None = generic mode.
        domain_guidance: DomainValidationGuidance | None = None,
        domain_id: str | None = None,
    ) -> ValidationSetDTO:
        """Build a validation set from the run's chunks + modality
 artifacts + (when wired) one whole-document LLM call grounded
 in `evidence_blocks` and shaped by `domain_guidance`.

 Always returns a set, even when `chunks`/`evidence_blocks` are
 empty — in that case only the smoke (and any domain-driven
 negatives) are emitted, which the runner exercises as smoke.
 That's the right signal for "the run produced nothing queryable."

 Emit order: smoke → modality (tables → images → graph) →
 LLM-generated grounded cases → domain-driven negative checks.
 Negatives sit at the end so a tester scanning the list sees the
 grounded questions first; the FE further groups by
 `validation_scope` for clarity.
 """
        opts = options or GenerationOptions()
        sampled = _sample_chunks(
            chunks,
            max_samples=_MAX_CHUNK_SAMPLES,
            must_include_ids=opts.must_include_chunk_ids,
        )

        cases: list[ValidationTestCaseDTO] = [
            _smoke_case(
                citation_required=opts.citation_required,
                domain_id=domain_id,
            ),
        ]

        # Modality cases. Each modality's count is bounded by both
        # `opts.max_*_cases` (per-modality) and the global
        # `opts.max_cases` ceiling. Order: tables → images → graph
        # for reproducible sequencing across regenerations.
        for table in (table_artifacts or [])[:opts.max_table_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_table_case(table, domain_id=domain_id))
        for visual in (visual_artifacts or [])[:opts.max_image_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_image_case(visual, domain_id=domain_id))
        for graph in (graph_artifacts or [])[:opts.max_graph_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_graph_case(graph, domain_id=domain_id))

        # Whole-document LLM generation. One call with the full
        # evidence list + domain guidance instead of N per-chunk
        # calls. When evidence is empty or no LLM is wired we fall
        # back to the deterministic heuristic (one generic question
        # per sampled chunk) so the generator still ships SOMETHING
        # for runs without a FAST endpoint.
        budget = max(0, opts.max_cases - len(cases))
        # Reserve room for negative checks at the end of the budget.
        # Each domain-driven negative consumes one slot; carve them
        # out up front so grounded cases respect both ceilings.
        negative_slots = _count_negative_slots(
            opts=opts, guidance=domain_guidance,
        )
        positive_budget = max(0, budget - negative_slots)

        llm_cases, llm_trace = self._llm_generate_grounded_cases(
            evidence_blocks=evidence_blocks or [],
            domain_guidance=domain_guidance,
            domain_id=domain_id,
            budget=positive_budget,
            citation_required=opts.citation_required,
        )
        if llm_cases:
            cases.extend(llm_cases)
        elif sampled and positive_budget > 0:
            # Fallback path: heuristic (no LLM / LLM failure).
            # Generates one generic-form question per sampled chunk.
            for chunk in sampled:
                if len(cases) >= opts.max_cases - negative_slots:
                    break
                heuristic = _heuristic_questions_for_chunk(
                    chunk, budget=_HEURISTIC_QUESTIONS_PER_CHUNK,
                )
                for q in heuristic:
                    if len(cases) >= opts.max_cases - negative_slots:
                        break
                    cases.append(_heuristic_case_from_question(
                        chunk=chunk,
                        question=q,
                        citation_required=opts.citation_required,
                        domain_id=domain_id,
                    ))

        # Domain-driven negative checks. Replaces the prior hardcoded
        # "World Cup / Bitcoin / Mars" pool with questions derived
        # from the domain pack's `negative_check_fields`. When no
        # pack is active we emit zero negatives — better than the
        # old pool, which testers saw as random off-topic questions.
        for neg in _domain_negative_cases(
            guidance=domain_guidance,
            domain_id=domain_id,
            limit=negative_slots,
        ):
            if len(cases) >= opts.max_cases:
                break
            cases.append(neg)

        artifacts_hash = _hash_chunks(sampled)
        context_summary = _build_context_summary(
            evidence_blocks=evidence_blocks or [],
            domain_guidance=domain_guidance,
            llm_called=llm_trace is not None and llm_trace.called,
        )
        return ValidationSetDTO(
            validation_set_id=f"vs-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            document_ids=list(document_ids),
            source="generated",
            status="draft",
            created_at=_iso_now(),
            created_by=actor,
            generator_version=GENERATOR_VERSION,
            artifacts_content_hash=artifacts_hash,
            test_cases=cases,
            metadata={
                "sampled_chunk_count": len(sampled),
                "total_chunks_available": len(chunks),
                "evidence_block_count": len(evidence_blocks or []),
                "table_artifact_count": len(table_artifacts or []),
                "visual_artifact_count": len(visual_artifacts or []),
                "graph_artifact_count": len(graph_artifacts or []),
            },
            domain_id=domain_id,
            llm=llm_trace,
            context_summary=context_summary,
        )

    # ---- Internals -----------------------------------------------------

    def _llm_generate_grounded_cases(
        self,
        *,
        evidence_blocks: list[EvidenceBlockDTO],
        domain_guidance: DomainValidationGuidance | None,
        domain_id: str | None,
        budget: int,
        citation_required: bool,
    ) -> tuple[list[ValidationTestCaseDTO], LLMTraceDTO | None]:
        """One whole-document LLM call. Returns the grounded cases
 plus a trace describing what happened (`None` when no LLM is
 wired — caller falls back to heuristics).

 Grounding contract: every emitted case must have a
 verbatim `evidence_quote` that appears in one of the provided
 evidence blocks. We enforce this server-side so a hallucinating
 model can't slip an off-topic case through — the filter drops
 any quote-less or fabricated-quote entries before they reach
 the response.
 """
        if budget <= 0:
            return ([], None)
        if self._text_client is None:
            return ([], None)
        if not evidence_blocks:
            # No body text reached us. Generating from an empty
            # evidence context is exactly how we ended up with the
            # "World Cup" world-knowledge drift before. Skip.
            return ([], LLMTraceDTO(called=False, error="no_evidence"))

        evidence_section = _format_evidence_for_prompt(evidence_blocks)
        guidance_section = _format_domain_guidance(domain_guidance)
        user_prompt = (
            f"Generate up to {budget} validation test cases.\n\n"
            f"Evidence context:\n{evidence_section}\n\n"
            f"Domain guidance context:\n{guidance_section}\n"
        )
        provider = getattr(self._text_client, "provider", None)
        model = getattr(self._text_client, "model", None)
        started = time.monotonic()
        try:
            parsed, usage = self._text_client.extract(
                user_prompt,
                _GENERATION_SCHEMA,
                metadata={
                    "caller": "validation.set_generator",
                    "system_prompt": _SYSTEM_PROMPT,
                },
            )
        except Exception as exc:  # noqa: BLE001 — record + fall back
            _log.warning(
                "validation question generation failed (provider=%s "
                "model=%s): %s", provider, model, exc,
            )
            return ([], LLMTraceDTO(
                called=True,
                provider=provider,
                model=model,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:512],
            ))

        latency_ms = int((time.monotonic() - started) * 1000)
        raw_cases = (
            parsed.get("test_cases") if isinstance(parsed, dict) else None
        )
        if not isinstance(raw_cases, list):
            return ([], LLMTraceDTO(
                called=True,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                error="malformed_response",
            ))

        cases: list[ValidationTestCaseDTO] = []
        evidence_by_id = {b.artifact_id: b for b in evidence_blocks}
        evidence_full_text = " ".join(b.text or "" for b in evidence_blocks)
        for raw in raw_cases[:budget]:
            case = _materialize_llm_case(
                raw=raw,
                evidence_by_id=evidence_by_id,
                evidence_full_text=evidence_full_text,
                domain_id=domain_id,
                citation_required=citation_required,
            )
            if case is not None:
                cases.append(case)

        trace = LLMTraceDTO(
            called=True,
            provider=getattr(usage, "provider", provider),
            model=getattr(usage, "model", model),
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "input_tokens", None) or None,
            completion_tokens=getattr(usage, "output_tokens", None) or None,
        )
        return (cases, trace)


# ---- Module-level helpers (unit-testable in isolation) -------------


def _smoke_case(
    *, citation_required: bool, domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """The canonical 'is the index alive?' smoke case. Shows up in
 every generated set so a tester always has a fast pass/fail
 signal even if the rest of the set is slow or fails."""
    return ValidationTestCaseDTO(
        test_case_id=f"tc-smoke-{uuid.uuid4().hex[:8]}",
        question="What is this document about?",
        type="retrieval",
        priority="smoke",
        expected_behavior="answer_with_citations",
        expected_answer_points=[],
        expected_chunks=[],
        citation_required=citation_required,
        source_traceability=[],
        metadata={"smoke": True},
        question_type="summary",
        validation_scope="generic",
        domain_id=domain_id,
    )


def _negative_case(
    question: str,
    *,
    expected_answer: str | None = None,
    field_label: str | None = None,
    domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """A domain-driven negative test case. The runner expects the
 engine to abstain (regex check) and not fabricate (judge check).
 `expected_chunks` / `expected_answer_points` are intentionally
 empty — there's nothing in the document to cite. The
 `expected_answer` is the standard abstention sentence so the
 FE can render a clear "expected: No" badge."""
    if expected_answer is None:
        target = field_label or "the requested information"
        expected_answer = (
            f"No. The provided evidence does not specify {target}."
        )
    return ValidationTestCaseDTO(
        test_case_id=f"tc-neg-{uuid.uuid4().hex[:8]}",
        question=question,
        type="negative",
        priority="normal",
        expected_behavior="abstain",
        expected_answer_points=[],
        expected_chunks=[],
        # Negative cases never require citations — an honest abstain
        # has none. Setting the flag would force a check failure
        # for the right behaviour.
        citation_required=False,
        source_traceability=[],
        metadata={
            "negative": True,
            "negative_check_field": field_label,
        },
        expected_answer=expected_answer,
        evidence_quote=None,
        source_artifact_id=None,
        source_artifact_type=None,
        question_type="missing_information_check",
        validation_scope="negative_check",
        domain_id=domain_id,
    )


# ---- modality case factories ------------------------------
#
# Each factory builds a case asserting that the named artifact is
# retrievable for a topic-related question. The question text is
# deterministic — derived from the artifact's title or kind — so
# regenerating produces identical case sequences. LLM-driven
# question phrasing (with chunk content) is a + enhancement.


def _table_case(
    artifact: ArtifactRecord, *, domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """A retrieval check for one extracted-table artifact."""
    label = _label_for_artifact(artifact)
    page_hint = _page_hint(artifact)
    return ValidationTestCaseDTO(
        test_case_id=f"tc-table-{uuid.uuid4().hex[:8]}",
        question=(
            f"What does the table on {page_hint} ({label}) show?"
            if page_hint
            else f"What information is in the table {label}?"
        ),
        type="table",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=[artifact.artifact_id],
        expected_pages=_pages_from_artifact(artifact),
        citation_required=True,
        source_traceability=[artifact.artifact_id],
        metadata={"table": True, "artifact_id": artifact.artifact_id},
        question_type="table_extraction",
        validation_scope="generic",
        source_artifact_id=artifact.artifact_id,
        source_artifact_type=artifact.kind,
        domain_id=domain_id,
    )


def _image_case(
    artifact: ArtifactRecord, *, domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """A retrieval check for one visual-content artifact."""
    label = _label_for_artifact(artifact)
    page_hint = _page_hint(artifact)
    return ValidationTestCaseDTO(
        test_case_id=f"tc-image-{uuid.uuid4().hex[:8]}",
        question=(
            f"What is shown in the image on {page_hint} ({label})?"
            if page_hint
            else f"What is depicted in the image {label}?"
        ),
        type="image",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=[artifact.artifact_id],
        expected_pages=_pages_from_artifact(artifact),
        citation_required=True,
        source_traceability=[artifact.artifact_id],
        metadata={"image": True, "artifact_id": artifact.artifact_id},
        question_type="fact_retrieval",
        validation_scope="generic",
        source_artifact_id=artifact.artifact_id,
        source_artifact_type=artifact.kind,
        domain_id=domain_id,
    )


def _graph_case(
    artifact: ArtifactRecord, *, domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """A retrieval check for graph snapshots.

 keeps this lightweight: we ask the engine to surface
 the document's main entities/relationships. The check verifies
 that retrieval engaged the graph artifact at all ( may
 add LLM-driven entity-specific question generation). When the
 graph artifact's metadata carries explicit `entity_ids` or
 `top_entities`, the case populates `expected_graph_nodes`."""
    expected_nodes = _expected_graph_nodes_from_artifact(artifact)
    return ValidationTestCaseDTO(
        test_case_id=f"tc-graph-{uuid.uuid4().hex[:8]}",
        question=(
            "What are the main entities and relationships described "
            "in this document?"
        ),
        type="graph",
        priority="normal",
        expected_behavior="validate_relationship",
        expected_artifacts=[artifact.artifact_id],
        expected_graph_nodes=expected_nodes,
        citation_required=True,
        source_traceability=[artifact.artifact_id],
        metadata={"graph": True, "artifact_id": artifact.artifact_id},
        question_type="reasoning_from_context",
        validation_scope="generic",
        source_artifact_id=artifact.artifact_id,
        source_artifact_type=artifact.kind,
        domain_id=domain_id,
    )


def _label_for_artifact(artifact: ArtifactRecord) -> str:
    """Short human-readable label for use inside generated questions.
 Prefers an explicit `title` from the artifact metadata, falls
 back to the kind/id pair so questions never reference an
 empty placeholder."""
    title = artifact.metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:80]
    return f"{artifact.kind}/{artifact.artifact_id[:8]}"


def _page_hint(artifact: ArtifactRecord) -> str:
    """Best-effort page reference. Reads any of the standard
 metadata keys producers use. Empty when no page info — caller
 branches on the empty string to choose the alternate question
 template."""
    for key in ("page", "page_start", "source_location"):
        value = artifact.metadata.get(key)
        if value not in (None, ""):
            return f"page {value}"
    return ""


def _pages_from_artifact(artifact: ArtifactRecord) -> list[int]:
    """Extract the artifact's page set as a list of ints, used by
 the runner's `expected_page_in_citations` check."""
    pages: list[int] = []
    for key in ("page_start", "page"):
        value = artifact.metadata.get(key)
        try:
            pages.append(int(value))
            break
        except (TypeError, ValueError):
            continue
    end = artifact.metadata.get("page_end")
    try:
        page_end = int(end) if end is not None else None
    except (TypeError, ValueError):
        page_end = None
    if pages and page_end is not None and page_end > pages[0]:
        pages = list(range(pages[0], page_end + 1))
    return pages


def _expected_graph_nodes_from_artifact(
    artifact: ArtifactRecord,
) -> list[str]:
    """Pull a list of expected graph node ids from the artifact's
 metadata when the producer surfaced one. Producers vary on
 the key — check the common variants. Returns [] when the
 artifact doesn't surface entity ids; the runner's
 `expected_graph_evidence` check then becomes a noop (omitted)
 rather than a vacuous failure.
 """
    for key in ("top_entities", "entity_ids", "expected_nodes"):
        value = artifact.metadata.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if v][:5]
    return []


def _sample_chunks(
    chunks: list[_ChunkRecord],
    *,
    max_samples: int,
    must_include_ids: tuple[str, ...] = (),
) -> list[_ChunkRecord]:
    """Pick a representative slice. Strategy:

 1. Always include the explicit `must_include_ids` ( will
 use this for incremental regeneration).
 2. Then evenly stride the remaining chunks. Even striding gives
 section diversity for free without a section-aware sort —
 the projector returns chunks in chunk_order already, so
 taking every Nth chunk hits the front, middle, and tail.
 """
    if not chunks:
        return []
    out: list[_ChunkRecord] = []
    seen: set[str] = set()
    by_id = {c.chunk_id: c for c in chunks if c.chunk_id}

    for cid in must_include_ids:
        c = by_id.get(cid)
        if c is not None and c.chunk_id not in seen:
            out.append(c)
            seen.add(c.chunk_id)
        if len(out) >= max_samples:
            return out

    remaining = max_samples - len(out)
    if remaining <= 0:
        return out
    candidates = [c for c in chunks if c.chunk_id and c.chunk_id not in seen]
    if not candidates:
        return out
    if len(candidates) <= remaining:
        out.extend(candidates)
        return out
    stride = max(1, len(candidates) // remaining)
    for i in range(0, len(candidates), stride):
        out.append(candidates[i])
        if len(out) >= max_samples:
            break
    return out[:max_samples]


def _heuristic_questions_for_chunk(
    chunk: _ChunkRecord, *, budget: int,
) -> list[dict[str, Any]]:
    """Deterministic question producer for the no-LLM / LLM-failed
 path. Picks the chunk's first sentence and asks the trivial
 'what does the document say about <first sentence>?' question.
 Crude but sufficient as a smoke baseline — the test still
 checks that the right chunk is retrieved, which is the actual
 contract."""
    body = (chunk.body or "").strip()
    if not body:
        return []
    sentence = _first_sentence(body, max_chars=140)
    if not sentence:
        return []
    return [{
        "question": (
            f"What does the document say about: {sentence}?"
        ),
        "type": "retrieval",
        "expected_answer_points": [],
    }][:budget]


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(body: str, *, max_chars: int) -> str:
    """Tokenise on sentence boundaries with a hard char-cap fallback.

 The cap protects against pathological inputs (a page of code
 with no punctuation would otherwise emit a 10k-char 'sentence').
 """
    body = body.strip()
    pieces = _SENTENCE_END_RE.split(body, maxsplit=1)
    first = pieces[0] if pieces else body
    return first[:max_chars].strip()


def _pages_for_chunk(chunk: _ChunkRecord) -> list[int]:
    """Inclusive page range from a chunk's `page_start` / `page_end`
 metadata. Returns [] when the producer didn't surface page
 info — the runner skips the page-level check rather than
 failing on a missing-data signal."""
    if chunk.page_start is None:
        return []
    if chunk.page_end is None or chunk.page_end == chunk.page_start:
        return [int(chunk.page_start)]
    return list(range(int(chunk.page_start), int(chunk.page_end) + 1))


def _hash_chunks(chunks: list[_ChunkRecord]) -> str:
    """Idempotency key input. Hashes the SAMPLED chunks' ids + body
 prefixes — same sample + same content → same hash, so the
 caller can cache by `(run_id, generator_version, hash)` to
 skip re-generation when nothing meaningful changed."""
    if not chunks:
        return "sha256:empty"
    h = hashlib.sha256()
    for c in chunks:
        h.update(((c.chunk_id or "") + ":").encode("utf-8"))
        body = (c.body or "")[:_PREVIEW_MAX_CHARS]
        h.update(body.encode("utf-8"))
        h.update(b"|")
    return f"sha256:{h.hexdigest()}"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---- New evidence-grounded helpers ---------------------------------


def _format_evidence_for_prompt(
    evidence: list[EvidenceBlockDTO],
) -> str:
    """Render evidence blocks as numbered context for the LLM. The
 same `[N]` numbering becomes the structured-output's evidence
 reference, so the LLM can copy the source_artifact_id verbatim
 from this block. Long blocks get truncated; total stays under
 `_MAX_EVIDENCE_TOTAL_CHARS`."""
    if not evidence:
        return "(no evidence available)"
    parts: list[str] = []
    used = 0
    for idx, block in enumerate(evidence, start=1):
        text = (block.text or "").strip()
        if not text:
            continue
        if len(text) > _MAX_EVIDENCE_BLOCK_CHARS:
            text = text[:_MAX_EVIDENCE_BLOCK_CHARS].rstrip() + "…"
        header_bits = [f"[{idx}]", f"source_artifact_id={block.artifact_id}"]
        if block.artifact_type:
            header_bits.append(f"artifact_type={block.artifact_type}")
        if block.page_start is not None:
            if block.page_end is not None and block.page_end != block.page_start:
                header_bits.append(f"pages={block.page_start}-{block.page_end}")
            else:
                header_bits.append(f"page={block.page_start}")
        if block.section:
            header_bits.append(f"section={block.section!r}")
        header = " ".join(header_bits)
        block_str = f"{header}\n{text}"
        if used + len(block_str) > _MAX_EVIDENCE_TOTAL_CHARS:
            break
        parts.append(block_str)
        used += len(block_str)
    return "\n\n".join(parts) if parts else "(no evidence available)"


def _format_domain_guidance(
    guidance: DomainValidationGuidance | None,
) -> str:
    """Render the domain pack's validation block as a TESTING-LENS
 rubric — explicitly NOT factual evidence. The prompt repeats
 this rule but having the rubric framed inside the user message
 too gives small local LLMs a stronger anchor."""
    if guidance is None or not guidance.enabled:
        return "(no domain guidance — generate generic questions only)"
    lines = [
        "Use the following ONLY as a rubric for choosing useful "
        "validation angles. It is NOT evidence and must NEVER create "
        "expected answers that are not supported by the evidence "
        "context above.",
    ]
    if guidance.question_types:
        lines.append(
            "Preferred question types: "
            + ", ".join(guidance.question_types),
        )
    if guidance.important_fields:
        lines.append(
            "Important fields to look for in the evidence: "
            + ", ".join(guidance.important_fields),
        )
    if guidance.negative_check_fields:
        lines.append(
            "If the evidence does NOT cover any of these fields, "
            "emit one missing_information_check question per missing "
            "field (validation_scope=\"negative_check\"): "
            + ", ".join(guidance.negative_check_fields),
        )
    return "\n".join(lines)


def _materialize_llm_case(
    *,
    raw: Any,
    evidence_by_id: dict[str, EvidenceBlockDTO],
    evidence_full_text: str,
    domain_id: str | None,
    citation_required: bool,
) -> ValidationTestCaseDTO | None:
    """Validate one LLM-emitted case and project it into a
 `ValidationTestCaseDTO`.

 The grounding contract has three filters:

   1. `question` and `expected_answer` are non-empty strings.
   2. For non-negative scopes: at least one `evidence` item with
      a `quote` that ACTUALLY appears in the supplied evidence
      text (case-insensitive substring match). Anti-hallucination.
   3. The cited `source_artifact_id` must match one of the
      evidence blocks we sent. If the LLM made one up, drop it.

 Returns `None` when any filter fails — the caller skips that
 case rather than emitting an ungrounded record."""
    if not isinstance(raw, dict):
        return None
    question = str(raw.get("question") or "").strip()
    expected_answer = str(raw.get("expected_answer") or "").strip()
    if not question or not expected_answer:
        return None

    scope_raw = str(raw.get("validation_scope") or "").strip().lower()
    scope = scope_raw if scope_raw in _ALLOWED_SCOPES else "generic"

    q_type_raw = str(raw.get("question_type") or "").strip().lower()
    question_type = q_type_raw if q_type_raw in _ALLOWED_QUESTION_TYPES else None

    difficulty = str(raw.get("difficulty") or "").strip() or None

    # Evidence list. Each entry must reference a known source
    # artifact_id; the quote must appear in the supplied evidence.
    quote: str | None = None
    source_artifact_id: str | None = None
    source_artifact_type: str | None = None
    items = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("source_artifact_id") or "").strip()
        candidate_quote = str(item.get("quote") or "").strip()
        if not candidate_id or not candidate_quote:
            continue
        if candidate_id not in evidence_by_id:
            # Cited artifact wasn't in the supplied evidence. The LLM
            # is hallucinating an id; skip this evidence item but
            # keep looking for a valid one.
            continue
        # Quote must actually appear in the evidence we sent. Use a
        # generous substring check (case-insensitive) — local LLMs
        # paraphrase punctuation/whitespace even when instructed to
        # quote verbatim, so we accept any sufficient substring of
        # the quote's first chunk that does land in the evidence.
        normalised = _normalise_for_match(candidate_quote)
        haystack = _normalise_for_match(evidence_full_text)
        # Try the full quote, then progressively shorter prefixes
        # down to ~40 chars so paraphrased trailing whitespace still
        # matches. Reject anything shorter — too easy to hallucinate
        # past a small substring.
        if not _quote_is_in_evidence(normalised, haystack):
            continue
        quote = candidate_quote
        source_artifact_id = candidate_id
        block = evidence_by_id[candidate_id]
        source_artifact_type = block.artifact_type
        break

    if scope == "negative_check":
        # Negative checks legitimately have no quote — they assert
        # the evidence is silent on something. Accept them without
        # the grounding filter; the runner expects abstention.
        return ValidationTestCaseDTO(
            test_case_id=f"tc-{uuid.uuid4().hex[:10]}",
            question=question,
            type="negative",
            priority="normal",
            expected_behavior="abstain",
            expected_answer_points=[],
            expected_chunks=[],
            citation_required=False,
            source_traceability=[],
            metadata={"llm_generated": True},
            expected_answer=expected_answer,
            evidence_quote=None,
            source_artifact_id=None,
            source_artifact_type=None,
            question_type=question_type or "missing_information_check",
            validation_scope="negative_check",
            difficulty=difficulty,
            domain_id=domain_id,
        )

    if quote is None or source_artifact_id is None:
        # Positive case without a verifiable evidence quote. This is
        # the anti-hallucination guard — drop it rather than ship a
        # case that may have come from outside-world knowledge.
        return None

    return ValidationTestCaseDTO(
        test_case_id=f"tc-{uuid.uuid4().hex[:10]}",
        question=question,
        # Runner-facing kind. Map LLM scope → runner type; default
        # to `answer` (the runner checks the answer for citations).
        type="answer",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer_points=[expected_answer] if expected_answer else [],
        expected_chunks=[],
        citation_required=citation_required,
        source_traceability=[source_artifact_id],
        metadata={"llm_generated": True},
        expected_answer=expected_answer,
        evidence_quote=quote,
        source_artifact_id=source_artifact_id,
        source_artifact_type=source_artifact_type,
        question_type=question_type,
        validation_scope=scope,
        difficulty=difficulty,
        domain_id=domain_id,
    )


# Normalisation regex for quote-in-evidence matching. Collapses
# whitespace runs and lowers case; punctuation passes through so
# we don't accidentally accept "Stage 1" as a substring of
# "Stage 12".
_WS_RE = re.compile(r"\s+")


def _normalise_for_match(text: str) -> str:
    return _WS_RE.sub(" ", text.strip().lower())


def _quote_is_in_evidence(quote: str, haystack: str) -> bool:
    """Substring match with progressive prefix-shortening so a
 paraphrased quote still grounds when the LLM trimmed trailing
 punctuation or whitespace. Minimum accepted prefix is 40 chars
 — shorter than that we'd risk accepting hallucinated quotes that
 happen to share a few words with the doc."""
    if not quote:
        return False
    if quote in haystack:
        return True
    cut = quote
    while len(cut) >= 40:
        if cut in haystack:
            return True
        cut = cut[: max(40, len(cut) - 20)]
        if len(cut) == 40 and cut not in haystack:
            return False
    return False


def _heuristic_case_from_question(
    *,
    chunk: _ChunkRecord,
    question: dict[str, Any],
    citation_required: bool,
    domain_id: str | None,
) -> ValidationTestCaseDTO:
    """Wrap one heuristic-question dict into a typed DTO. Mirrors the
 old `_cases_for_chunk` shape so existing tests that exercised
 the heuristic fallback path keep passing."""
    return ValidationTestCaseDTO(
        test_case_id=f"tc-{uuid.uuid4().hex[:10]}",
        question=question["question"],
        type=question.get("type", "retrieval"),
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer_points=list(question.get("expected_answer_points") or []),
        expected_chunks=[chunk.chunk_id] if chunk.chunk_id else [],
        expected_pages=_pages_for_chunk(chunk),
        citation_required=citation_required,
        source_traceability=[chunk.chunk_id] if chunk.chunk_id else [],
        metadata={
            "section": chunk.section,
            "title": chunk.title,
            "heuristic": True,
        },
        question_type="fact_retrieval",
        validation_scope="generic",
        source_artifact_id=chunk.source_artifact_id,
        source_artifact_type="chunk",
        domain_id=domain_id,
    )


def _count_negative_slots(
    *,
    opts: GenerationOptions,
    guidance: DomainValidationGuidance | None,
) -> int:
    """How many domain-driven negative cases we should emit. Zero
 when no guidance is available — we never fall back to the old
 hardcoded sports/celebrity pool. Bounded by both the request's
 `negative_case_count` and the pack's declared fields."""
    if guidance is None or not guidance.enabled:
        return 0
    if not guidance.allow_negative_checks_from_domain_checklist:
        return 0
    available = len(guidance.negative_check_fields)
    requested = max(0, opts.negative_case_count)
    return min(available, requested)


def _domain_negative_cases(
    *,
    guidance: DomainValidationGuidance | None,
    domain_id: str | None,
    limit: int,
) -> list[ValidationTestCaseDTO]:
    """Emit domain-driven negative checks. One question per field in
 `negative_check_fields`, capped at `limit`. Each question asks
 'Does the document specify <field>?' and carries the standard
 abstention sentence as `expected_answer`."""
    if limit <= 0 or guidance is None or not guidance.enabled:
        return []
    fields = list(guidance.negative_check_fields)[:limit]
    cases: list[ValidationTestCaseDTO] = []
    for field_id in fields:
        # Operator-readable label: "soil_or_geotechnical_information"
        # → "soil or geotechnical information". Keeps domain.yaml
        # config in snake_case while questions stay readable.
        label = field_id.replace("_", " ").strip()
        if not label:
            continue
        question = f"Does the document specify {label}?"
        cases.append(_negative_case(
            question=question,
            expected_answer=(
                f"No. The provided evidence does not specify {label}."
            ),
            field_label=label,
            domain_id=domain_id,
        ))
    return cases


def _build_context_summary(
    *,
    evidence_blocks: list[EvidenceBlockDTO],
    domain_guidance: DomainValidationGuidance | None,
    llm_called: bool,
) -> dict[str, Any]:
    """Operator-readable summary of what the generator passed to
 the LLM. Surfaced on the response so testers can verify "the
 model actually saw the document" without re-running."""
    sources: list[str] = []
    seen: set[str] = set()
    for b in evidence_blocks:
        kind = b.artifact_type or ""
        if kind and kind not in seen:
            seen.add(kind)
            sources.append(kind)
    total_chars = sum(len(b.text or "") for b in evidence_blocks)
    return {
        "evidence_sources_used": sources,
        "evidence_block_count": len(evidence_blocks),
        "evidence_char_count": total_chars,
        "domain_guidance_used": (
            domain_guidance is not None and domain_guidance.enabled
        ),
        "llm_called": llm_called,
    }
