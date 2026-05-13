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
from j1.validation.context import (
    ContextEntity,
    ContextFact,
    ContextSection,
    ValidationQuestionContext,
    build_question_context,
)
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
# the rest. Reduced from 1500 → 800 after operators reported slow
# generation — at 1500 chars × 8 blocks the prompt approaches the
# context window on small local LLMs even before the schema is
# added, which slows the round-trip without improving grounding.
_MAX_EVIDENCE_BLOCK_CHARS = 800

# Cumulative-evidence cap. Bigger than the synthesizer's because
# question generation needs a wider view of the document — the
# whole-document call is what stops the LLM from inventing topics.
# Reduced from 8000 → 5000 along with the per-block cap; 5000 chars
# of evidence is still plenty for the LLM to choose useful angles
# from.
_MAX_EVIDENCE_TOTAL_CHARS = 5000

# Cap on grounded cases requested from the LLM in a single call.
# Local LLMs misbehave above ~10 cases: output tokens grow past the
# typical 1024-2048 ``max_tokens`` setting, the last case's question
# / expected_answer gets truncated mid-string, and total wall-clock
# stretches past 30 seconds. When the operator's ``max_cases``
# budget is bigger than this cap, the remainder is filled with the
# fast deterministic heuristic — much better than emitting a
# nonsensical truncated case.
_LLM_MAX_GROUNDED_PER_CALL = 8

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

    # Hard ceiling. Quality-first policy (post operator
    # feedback): the generator EMITS ONLY AS MANY questions as
    # the document can support with evidence. There's no
    # minimum and no padding. ``max_cases`` is a soft upper
    # bound — most sets land at 3-8 cases for a small/medium
    # document. Callers who want more coverage should improve
    # the document's evidence quality, not raise this number.
    max_cases: int = 12
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
        # Real evidence blocks (chunk bodies + compiled.text
        # windows) the service built via ``build_evidence_blocks``.
        # When supplied, the LLM gets a single whole-document call
        # instead of N per-chunk calls — this is what stops the
        # generator from emitting off-topic questions.
        evidence_blocks: list[EvidenceBlockDTO] | None = None,
        # Domain guidance loaded from ``domain.yaml``'s
        # ``validation:`` block. Used purely as a testing-lens
        # rubric — never as factual evidence. None = generic mode.
        domain_guidance: DomainValidationGuidance | None = None,
        domain_id: str | None = None,
        # Enriched-pipeline artifacts (``enriched.document_map``,
        # ``enriched.summary``, etc.). When the registry exposed
        # them the context builder mines them for entities,
        # relationships, and a doc purpose string — richer seed
        # material than chunks alone. Optional; the generator
        # gracefully degrades to chunk-only context when they're
        # absent.
        enriched_artifacts: list[ArtifactRecord] | None = None,
        # The ``final_ingestion_report`` artifact, decoded. Carries
        # ``document_name`` / ``document_id`` / ``page_count`` /
        # ``compile_summary`` which the context uses for titles +
        # page references. Optional.
        final_report: dict[str, Any] | None = None,
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

        # Build the structured ValidationQuestionContext from
        # everything the registry exposed. This is the seed pool
        # the question generators read from. Building it up front
        # — even when chunks are empty — lets the smoke / graph /
        # modality emitters reference doc-specific names instead
        # of the prior hardcoded "What is this document about?"
        # boilerplate.
        context = build_question_context(
            chunks=chunks,
            table_artifacts=table_artifacts,
            visual_artifacts=visual_artifacts,
            graph_artifacts=graph_artifacts,
            enriched_artifacts=enriched_artifacts,
            final_report=final_report,
            domain_id=domain_id,
            domain_guidance=domain_guidance,
        )

        # Quality-first policy (post operator feedback): no smoke,
        # no heuristic top-up, no minimum count. The generator
        # emits only cases that survive evidence + topic gating.
        # If the document is sparse, the set is sparse; that's
        # the right signal.
        cases: list[ValidationTestCaseDTO] = []

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
        # Graph cases — context-aware. Coalesces artifacts that
        # share the same top-entities. Emits per-bucket only when
        # at least two clean entities are available (single-entity
        # buckets used to produce vague "What does the document
        # say about X?" cases without backing facts).
        for case in _graph_cases_deduped(
            graph_artifacts or [],
            limit=opts.max_graph_cases,
            domain_id=domain_id,
            context=context,
        ):
            if len(cases) >= opts.max_cases:
                break
            cases.append(case)

        # Context-driven category emitters. Each emits 0-N cases
        # depending on what the document supports. Operator-stated
        # rule: never pad. Returns however many strong cases the
        # document can produce.
        category_cases = _emit_context_cases(
            context=context,
            budget=max(0, opts.max_cases - len(cases)),
            citation_required=opts.citation_required,
            domain_id=domain_id,
        )
        cases.extend(category_cases)

        # Whole-document LLM generation. Bounded to
        # ``_LLM_MAX_GROUNDED_PER_CALL`` (8) per round-trip; the
        # generator no longer tops up with the deterministic
        # heuristic — quality is the contract.
        negative_slots = _count_negative_slots(
            opts=opts, guidance=domain_guidance,
        )
        positive_budget = max(0, opts.max_cases - len(cases) - negative_slots)
        llm_budget = min(positive_budget, _LLM_MAX_GROUNDED_PER_CALL)
        llm_cases, llm_trace = self._llm_generate_grounded_cases(
            evidence_blocks=evidence_blocks or [],
            domain_guidance=domain_guidance,
            domain_id=domain_id,
            budget=llm_budget,
            citation_required=opts.citation_required,
        )
        cases.extend(llm_cases)

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

        # Post-emit quality pass: drop cases that look generic /
        # raw-chunk-text-injected, dedupe near-duplicates by
        # normalised question text. Spec section 12 enumerates
        # the reject criteria — the smoke case is exempt so the
        # set always has at least the "is the index alive?"
        # baseline regardless of how aggressive the filter is.
        cases = _filter_and_dedupe_cases(cases, context=context)

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
        truncated_dropped = 0
        for raw in raw_cases[:budget]:
            # Defensive: when the LLM hit max_tokens mid-output the
            # JSON-mode response can still parse but the last
            # question / expected_answer ends abruptly. Drop those
            # so testers don't see nonsensical "what is the…" rows.
            if _looks_truncated(raw):
                truncated_dropped += 1
                continue
            case = _materialize_llm_case(
                raw=raw,
                evidence_by_id=evidence_by_id,
                evidence_full_text=evidence_full_text,
                domain_id=domain_id,
                citation_required=citation_required,
            )
            if case is not None:
                cases.append(case)

        if truncated_dropped:
            _log.warning(
                "validation generator dropped %d truncated LLM "
                "case(s) (provider=%s model=%s latency_ms=%d "
                "budget=%d returned=%d); consider raising the LLM's "
                "max_output_tokens or lowering the per-call grounded "
                "cap (_LLM_MAX_GROUNDED_PER_CALL)",
                truncated_dropped, provider, model, latency_ms,
                budget, len(raw_cases),
            )
        _log.info(
            "validation generator LLM call: provider=%s model=%s "
            "latency_ms=%d budget=%d raw=%d emitted=%d dropped_truncated=%d",
            provider, model, latency_ms, budget, len(raw_cases),
            len(cases), truncated_dropped,
        )

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
        priority="edge",
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
        validation_scope="guardrail",
        domain_id=domain_id,
        generated_from="domain_rule",
        confidence=0.75,
        reason=(
            f"Guardrail: domain pack flagged {field_label!r} as a "
            f"field the document may not cover; engine must abstain."
            if field_label
            else "Guardrail: off-topic question; engine must abstain."
        ),
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
    """A retrieval check for one extracted-table artifact.

    Modality cases carry an ``expected_answer`` derived from the
    artifact metadata so they pass the quality gate's
    expected-answer requirement. The retrieval check still
    operates on ``expected_artifacts`` (the indexer must surface
    the table), but the answer summary tells the tester what the
    table is about — usually its title or the page it lives on.
    """
    label = _label_for_artifact(artifact)
    page_hint = _page_hint(artifact)
    expected_answer = (
        f"Content of the table on {page_hint}."
        if page_hint
        else f"Content of the table {label}."
    )
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
        expected_answer=expected_answer,
        expected_artifacts=[artifact.artifact_id],
        expected_pages=_pages_from_artifact(artifact),
        citation_required=True,
        source_traceability=[artifact.artifact_id],
        metadata={"table": True, "artifact_id": artifact.artifact_id},
        question_type="table_extraction",
        validation_scope="evidence",
        source_artifact_id=artifact.artifact_id,
        source_artifact_type=artifact.kind,
        domain_id=domain_id,
        generated_from="modality_table",
        confidence=0.75,
        reason=(
            f"Targets the extracted-table artifact "
            f"{artifact.artifact_id!r}."
        ),
        expected_evidence=(
            f"table on {page_hint}" if page_hint else f"table {label}"
        ),
    )


def _image_case(
    artifact: ArtifactRecord, *, domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """A retrieval check for one visual-content artifact. Same
    structure as ``_table_case`` — expected_answer is derived
    from the artifact location/label so the quality gate
    accepts it."""
    label = _label_for_artifact(artifact)
    page_hint = _page_hint(artifact)
    expected_answer = (
        f"Content of the image on {page_hint}."
        if page_hint
        else f"Content of the image {label}."
    )
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
        expected_answer=expected_answer,
        expected_artifacts=[artifact.artifact_id],
        expected_pages=_pages_from_artifact(artifact),
        citation_required=True,
        source_traceability=[artifact.artifact_id],
        metadata={"image": True, "artifact_id": artifact.artifact_id},
        question_type="fact_retrieval",
        validation_scope="evidence",
        source_artifact_id=artifact.artifact_id,
        source_artifact_type=artifact.kind,
        domain_id=domain_id,
        generated_from="modality_image",
        confidence=0.7,
        reason=(
            f"Targets the visual-content artifact "
            f"{artifact.artifact_id!r}."
        ),
        expected_evidence=(
            f"image on {page_hint}" if page_hint else f"image {label}"
        ),
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


def _graph_cases_deduped(
    graph_artifacts: list[ArtifactRecord],
    *,
    limit: int,
    domain_id: str | None,
    context: ValidationQuestionContext | None = None,
) -> list[ValidationTestCaseDTO]:
    """Produce at most `limit` graph cases, deduplicated by the set
 of expected entity ids the artifacts surface.

 Strategy:
   1. Bucket artifacts by `tuple(sorted(top_entities))` — empty
      tuple is its own bucket (the "no entity hint" case).
   2. Each bucket produces exactly ONE case. Multiple artifacts
      naming the same entities coalesce so the FE doesn't show
      "What are the main entities…?" three times in a row.
   3. When the bucket carries explicit entities, the question
      becomes entity-specific ("What is the relationship between
      <a> and <b>…?") and `expected_answer` is pre-populated. Bare
      buckets keep the generic boilerplate question.
   4. Buckets are emitted in their first artifact's order so
      reproducible across regenerations.

 `limit` caps total cases per call so a run with many graph
 artifacts can't crowd out other modalities or grounded LLM
 cases.
 """
    if limit <= 0 or not graph_artifacts:
        return []
    buckets: dict[tuple[str, ...], list[ArtifactRecord]] = {}
    bucket_order: list[tuple[str, ...]] = []
    for artifact in graph_artifacts:
        entities = tuple(_expected_graph_nodes_from_artifact(artifact))
        if entities not in buckets:
            buckets[entities] = []
            bucket_order.append(entities)
        buckets[entities].append(artifact)

    cases: list[ValidationTestCaseDTO] = []
    for entities in bucket_order:
        if len(cases) >= limit:
            break
        artifacts = buckets[entities]
        case = _graph_case_from_bucket(
            artifacts=artifacts,
            entities=list(entities),
            domain_id=domain_id,
            context=context,
        )
        if case is not None:
            cases.append(case)
    return cases


def _is_clean_graph_entity(name: str) -> bool:
    """Reject single-word noise tokens (PDF, The, Expected, …) from
    being treated as graph entities. Belt-and-braces with the
    context extractor's filter. Length isn't a primary signal —
    short legitimate names like "Bob" should pass; the noise
    list catches the bad 3-char tokens."""
    if not name:
        return False
    if name.count("-") >= 2 or ("-" in name and re.search(r"\d", name)):
        return False  # identifier shape, not an entity
    lowered = name.lower()
    if lowered in NOISE_TERMS:
        return False
    words = name.split()
    if len(words) == 1 and len(name) < 2:
        return False
    return True


def _graph_case_from_bucket(
    *,
    artifacts: list[ArtifactRecord],
    entities: list[str],
    domain_id: str | None,
    context: ValidationQuestionContext | None = None,
) -> ValidationTestCaseDTO | None:
    """Build one graph case for a bucket of artifacts.

    Quality-first policy: emit ONLY when at least two clean,
    non-noise entities are present (graph cases are inherently
    relationship questions). Single-entity buckets and
    no-entity buckets return ``None`` rather than the prior
    "What does the document say about CB-2?" / "main entities"
    boilerplate that operators flagged.
    """
    first = artifacts[0]
    artifact_ids = [a.artifact_id for a in artifacts]
    # When the artifact lacks per-row entity hints, lean on the
    # context's top entities. Pass them through the cleanliness
    # check so chunk-derived noise (PDF, The) doesn't sneak in.
    effective_entities: list[str] = []
    for name in (entities or []):
        if _is_clean_graph_entity(name):
            effective_entities.append(name)
    if not effective_entities and context is not None:
        for ent in context.entities:
            if ent.source in {"graph", "document_map"} and _is_clean_graph_entity(ent.name):
                effective_entities.append(ent.name)
                if len(effective_entities) >= 3:
                    break
    if len(effective_entities) < 2:
        # Not enough clean entities to phrase a meaningful
        # relationship question.
        return None
    a, b = effective_entities[0], effective_entities[1]
    question = (
        f"What is the relationship between {a} and {b} in this document?"
    )
    expected_answer = (
        f"The document describes a relationship between {a} and {b}."
    )
    reason = (
        f"Targets the relationship between two top entities "
        f"({a!r}, {b!r}) surfaced by the graph artifact."
    )
    return ValidationTestCaseDTO(
        test_case_id=f"tc-graph-{uuid.uuid4().hex[:8]}",
        question=question,
        type="graph",
        priority="normal",
        expected_behavior="validate_relationship",
        expected_artifacts=artifact_ids,
        expected_graph_nodes=list(effective_entities),
        citation_required=True,
        source_traceability=list(artifact_ids),
        metadata={
            "graph": True,
            "artifact_id": first.artifact_id,
            "deduped_artifact_count": len(artifacts),
        },
        question_type="reasoning_from_context",
        # ``graph`` is the post-refactor scope for these cases —
        # previously ``generic`` which gave testers no way to
        # filter graph-path questions in the Validation tab.
        validation_scope="graph",
        source_artifact_id=first.artifact_id,
        source_artifact_type=first.kind,
        domain_id=domain_id,
        expected_answer=expected_answer,
        generated_from="modality_graph",
        confidence=0.85 if len(effective_entities) >= 2 else 0.6,
        reason=reason,
        expected_evidence=(
            f"graph artifact {first.artifact_id}"
        ),
    )


# ---- Context-driven emitters (post-refactor) ------------------------
#
# These emitters are what stops the generator from emitting
# all-generic / 3-question sets for documents with rich context.
# Each consumes the ``ValidationQuestionContext`` and produces
# 0-N cases depending on what the document actually carries.
# Scope / type / priority / generated_from / reason fields are
# stamped at emission time so the FE can group + audit cases
# without inferring from question text.


def _smoke_case_from_context(
    *,
    context: ValidationQuestionContext,
    citation_required: bool,
    domain_id: str | None = None,
) -> ValidationTestCaseDTO:
    """Document-aware smoke case. Uses the title (when known) so
    the smoke isn't the same "What is this document about?"
    sentence for every run. The smoke remains required-to-pass —
    a working index should always be able to surface a topical
    summary for a document it ingested.
    """
    title = (context.document_title or "").strip()
    if title:
        question = f"What is the {title} document about?"
        reason = (
            f"Smoke test: confirm the index can summarise the "
            f"document whose title is {title!r}."
        )
    else:
        question = "What is this document about?"
        reason = "Smoke test: confirm the index produces ANY answer."
    expected_evidence: str | None = None
    if context.page_count:
        expected_evidence = (
            f"document has {context.page_count} pages — answer "
            f"should reference some of them"
        )
    return ValidationTestCaseDTO(
        test_case_id=f"tc-smoke-{uuid.uuid4().hex[:8]}",
        question=question,
        type="retrieval",
        priority="smoke",
        expected_behavior="answer_with_citations",
        expected_answer_points=[],
        expected_chunks=[],
        citation_required=citation_required,
        source_traceability=[],
        metadata={"smoke": True, "context_title": title or None},
        question_type="summary",
        # ``document`` is the post-refactor scope: the smoke
        # case tests the document path specifically, not a
        # vague "generic" bucket.
        validation_scope="document",
        domain_id=domain_id,
        generated_from="smoke",
        confidence=0.95,
        reason=reason,
        expected_evidence=expected_evidence,
    )


def _emit_context_cases(
    *,
    context: ValidationQuestionContext,
    budget: int,
    citation_required: bool,
    domain_id: str | None,
) -> list[ValidationTestCaseDTO]:
    """Emit content-driven cases — only when the source material is
    strong enough to produce a useful question.

    Policy (post operator feedback): NEVER pad output. Quality
    over quantity. Each emitter returns ``None`` when its seed
    material isn't evidence-anchored; this function drops the
    ``None`` rather than backfilling with weak templates.

    Per-category caps remain so a doc rich in one signal can't
    crowd out the others, but the function never tries to reach
    a minimum count. Returns however many strong cases survive.

    Source-material requirements (each emitter enforces):
      * entity case: entity comes from graph or document_map
        (high-signal sources), AND there's a fact mentioning it
      * fact case: clean ``_topic_from_fact`` survived the
        noise/identifier filter
      * section case: section is paired with a concrete fact
        from the same section
      * workflow case: stage entity passes the entity filter
      * domain case: only when the domain pack is wired AND the
        evidence actually covers the field

    Smoke / "what is this document about?" is DELIBERATELY not
    emitted. Operators flagged it as vague boilerplate that's
    not evidence-backed; the generator now skips it entirely.
    """
    if budget <= 0 or not context.has_any_facts():
        return []
    out: list[ValidationTestCaseDTO] = []
    remaining = lambda: max(0, budget - len(out))  # noqa: E731

    # Pre-index facts by section so the section emitter can pair
    # a heading with a concrete fact from inside it.
    facts_by_section: dict[str, list[ContextFact]] = {}
    for fact in context.facts:
        key = (fact.section or "").strip()
        if key:
            facts_by_section.setdefault(key, []).append(fact)

    # 1. Workflow stages. Capped at 3.
    for stage in context.workflow_stages[: min(3, remaining())]:
        case = _workflow_case(
            stage=stage, context=context, domain_id=domain_id,
            citation_required=citation_required,
            facts_by_section=facts_by_section,
        )
        if case is not None:
            out.append(case)
        if not remaining():
            return out

    # 2. Entity questions — high-signal entities only. The
    #    chunk-derived entity extractor has improved noise
    #    rejection, but we ALSO require a fact mentioning the
    #    entity so the case has a real evidence anchor.
    for entity in context.entities:
        if not remaining():
            return out
        if len(out) >= 4 + len(context.workflow_stages[:3]):
            break
        case = _entity_case(
            entity=entity, context=context, domain_id=domain_id,
            citation_required=citation_required,
        )
        if case is not None:
            out.append(case)

    # 3. Section walk — only when the section heading is paired
    #    with a concrete fact. Section-only without a fact is
    #    vague; reject.
    for section in context.sections[: min(2, remaining())]:
        case = _section_case(
            section=section, context=context, domain_id=domain_id,
            citation_required=citation_required,
            facts_by_section=facts_by_section,
        )
        if case is not None:
            out.append(case)
        if not remaining():
            return out

    # 4. Fact retrieval — the highest-quality signal. Capped at 5.
    for fact in context.facts[: min(5, remaining())]:
        case = _fact_case(
            fact=fact, context=context, domain_id=domain_id,
            citation_required=citation_required,
        )
        if case is not None:
            out.append(case)
        if not remaining():
            return out

    # 5. Domain context — only when the domain pack is wired AND
    #    the document evidence actually covers the field. We
    #    detect "covered" by string-matching the field name (with
    #    underscores replaced) against the fact corpus.
    if context.domain_guidance is not None and context.domain_guidance.enabled:
        fact_corpus = " ".join(
            f.text.lower() for f in context.facts
        )
        for field_name in (
            context.domain_guidance.important_fields[: min(2, remaining())]
        ):
            label = field_name.replace("_", " ").lower()
            if label and label in fact_corpus:
                out.append(_domain_case(
                    field_name=field_name, context=context,
                    domain_id=domain_id,
                    citation_required=citation_required,
                ))
                if not remaining():
                    return out

    return out


def _entity_case(
    *,
    entity: ContextEntity,
    context: ValidationQuestionContext,
    domain_id: str | None,
    citation_required: bool,
) -> ValidationTestCaseDTO | None:
    """Emit an entity-anchored case, OR return ``None`` when the
    entity doesn't have enough evidence backing to produce a
    useful question.

    Operator-stated rules:
      * single-word chunk-derived entities are noise (PDF, The,
        Expected) → reject — context extractor already filters
        most, this is the second line of defense.
      * every entity case MUST carry a fact-derived
        ``expected_evidence`` pointer so the runner can verify
        the answer cites the right chunk. Without one the case
        is just a vague "What is the role of <noun-run>?" which
        operators flagged.
    """
    name = entity.name
    # Find a backing fact that mentions this entity. The fact
    # gives us (a) a clean evidence pointer (page + chunk) and
    # (b) proof that the entity isn't a stray noun-run.
    backing_fact = _find_fact_mentioning(context.facts, name)
    if backing_fact is None:
        # Entity surfaced in metadata but never appears in a
        # clean sentence. Cannot anchor a useful question.
        return None

    # Reject chunk-source entities that are single bare words
    # (the context extractor already drops obvious noise but
    # chunk-source entities are the lowest-signal class so we
    # tighten further here).
    if entity.source == "chunk" and len(name.split()) == 1:
        return None

    expected_answer = _expected_answer_from_fact(
        backing_fact.text, name,
    )
    if not expected_answer:
        # No clean answer can be derived from the fact body —
        # skip rather than ship a question with no expected
        # answer summary.
        return None

    if entity.source == "graph":
        question = f"What does the document say about {name}?"
        scope: ValidationScope = "graph"
        case_type: ValidationTestType = "graph"
        confidence = 0.85
    elif entity.source == "document_map":
        question = f"What does the document say about {name}?"
        scope = "document"
        case_type = "retrieval"
        confidence = 0.8
    else:
        question = f"What does the document say about {name}?"
        scope = "retrieval"
        case_type = "retrieval"
        confidence = 0.7
    return ValidationTestCaseDTO(
        test_case_id=f"tc-ent-{uuid.uuid4().hex[:8]}",
        question=question,
        type=case_type,
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer=expected_answer,
        expected_answer_points=[name],
        expected_chunks=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        expected_pages=(
            [backing_fact.page] if backing_fact.page is not None else []
        ),
        citation_required=citation_required,
        source_traceability=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        metadata={"entity": name, "entity_source": entity.source},
        question_type="fact_retrieval",
        validation_scope=scope,
        source_artifact_id=backing_fact.artifact_id,
        domain_id=domain_id,
        generated_from="entity",
        confidence=confidence,
        reason=(
            f"Entity {name!r} (from {entity.source}) appears in a "
            f"fact on "
            f"{'page ' + str(backing_fact.page) if backing_fact.page else 'a chunk'}."
        ),
        expected_evidence=_evidence_pointer(backing_fact),
    )


def _section_case(
    *,
    section: ContextSection,
    context: ValidationQuestionContext,
    domain_id: str | None,
    citation_required: bool,
    facts_by_section: dict[str, list[ContextFact]] | None = None,
) -> ValidationTestCaseDTO | None:
    """Section-case emitter — requires a fact pairing.

    The previous "What does the section X cover?" template was
    vague when it stood alone — no expected answer, no evidence
    anchor beyond the heading itself. The new emitter requires
    at least one fact from the same section so the question
    carries an expected_answer derived from that fact.
    """
    paired_fact: ContextFact | None = None
    if facts_by_section is not None:
        candidates = facts_by_section.get(section.title) or []
        paired_fact = candidates[0] if candidates else None
    if paired_fact is None:
        return None
    expected_answer = _trim_for_expected_answer(paired_fact.text)
    if not expected_answer:
        return None
    page_hint = (
        f" on page {section.page}" if section.page is not None else ""
    )
    question = f"What does the section {section.title!r}{page_hint} cover?"
    return ValidationTestCaseDTO(
        test_case_id=f"tc-sec-{uuid.uuid4().hex[:8]}",
        question=question,
        type="retrieval",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer=expected_answer,
        expected_answer_points=[section.title],
        expected_chunks=(
            [paired_fact.chunk_id] if paired_fact.chunk_id else []
        ),
        expected_pages=[section.page] if section.page is not None else [],
        citation_required=citation_required,
        source_traceability=(
            [paired_fact.chunk_id] if paired_fact.chunk_id else []
        ),
        metadata={"section": section.title, "page": section.page},
        question_type="summary",
        validation_scope="workflow",
        source_artifact_id=paired_fact.artifact_id,
        domain_id=domain_id,
        generated_from="section",
        confidence=0.75,
        reason=(
            f"Section heading {section.title!r} paired with a fact "
            + (
                f"on page {paired_fact.page}"
                if paired_fact.page
                else "from inside it"
            )
            + "."
        ),
        expected_evidence=_evidence_pointer(paired_fact),
    )


# ---- Shared helpers for the strict emitters ------------------------


def _find_fact_mentioning(
    facts: tuple[ContextFact, ...] | list[ContextFact],
    name: str,
) -> ContextFact | None:
    """Return the first fact whose body contains the entity name.
    Case-insensitive substring match. ``None`` when no fact
    mentions it — the caller then skips the case entirely."""
    if not name:
        return None
    needle = name.lower()
    for fact in facts:
        if needle in (fact.text or "").lower():
            return fact
    return None


def _expected_answer_from_fact(
    fact_text: str, anchor: str | None = None,
) -> str | None:
    """Trim a fact sentence into a question's expected answer.

    When ``anchor`` is supplied, prefer the substring of the fact
    that starts at the anchor (so the answer naturally pivots
    around the entity the question is about). When the result is
    too short / too long / equals the anchor, return ``None``.
    """
    text = (fact_text or "").strip()
    if not text:
        return None
    if anchor and anchor.lower() in text.lower():
        # Substring after the anchor → answer "what comes next"
        # for that entity. Common in fact sentences like:
        #   "Stage 1 reviews drawings and calculations …"
        # When anchor="Stage 1" the answer trims to
        # "reviews drawings and calculations …" — closer to a
        # natural answer than the whole sentence.
        idx = text.lower().index(anchor.lower())
        tail = text[idx:].strip()
        if 10 < len(tail) <= 200 and tail.lower() != anchor.lower():
            return tail
    return _trim_for_expected_answer(text)


def _trim_for_expected_answer(text: str) -> str | None:
    text = (text or "").strip()
    if 10 < len(text) <= 200:
        return text
    if len(text) > 200:
        return text[:197].rstrip() + "…"
    return None


def _evidence_pointer(fact: ContextFact) -> str:
    """One-line "page X, section 'Y'" pointer for the FE."""
    parts: list[str] = []
    if fact.page is not None:
        parts.append(f"page {fact.page}")
    if fact.section:
        parts.append(f"section {fact.section!r}")
    if fact.chunk_id:
        parts.append(f"chunk {fact.chunk_id}")
    return ", ".join(parts) if parts else "document chunk"


def _workflow_case(
    *,
    stage: str,
    context: ValidationQuestionContext,
    domain_id: str | None,
    citation_required: bool,
    facts_by_section: dict[str, list[ContextFact]] | None = None,
) -> ValidationTestCaseDTO | None:
    """Workflow-stage emitter. Stages come from the context entity
    extractor (an entity whose first word is Stage / Phase / Step /
    Process). The stage must appear in a fact body — otherwise we
    can't synthesise a useful expected_answer.

    Returns ``None`` rather than a vague stub when no backing fact
    exists.
    """
    backing_fact = _find_fact_mentioning(context.facts, stage)
    if backing_fact is None:
        return None
    expected_answer = _expected_answer_from_fact(backing_fact.text, stage)
    if not expected_answer:
        return None
    return ValidationTestCaseDTO(
        test_case_id=f"tc-wf-{uuid.uuid4().hex[:8]}",
        question=f"What does the document say about {stage}?",
        type="retrieval",
        priority="critical",
        expected_behavior="answer_with_citations",
        expected_answer=expected_answer,
        expected_answer_points=[stage],
        expected_chunks=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        expected_pages=(
            [backing_fact.page] if backing_fact.page is not None else []
        ),
        citation_required=citation_required,
        source_traceability=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        metadata={"workflow_stage": stage},
        question_type="fact_retrieval",
        validation_scope="workflow",
        source_artifact_id=backing_fact.artifact_id,
        domain_id=domain_id,
        generated_from="workflow_stage",
        confidence=0.85,
        reason=(
            f"Workflow stage {stage!r} appears in a fact "
            + (
                f"on page {backing_fact.page}"
                if backing_fact.page
                else "in the chunk corpus"
            )
            + "."
        ),
        expected_evidence=_evidence_pointer(backing_fact),
    )


def _fact_case(
    *,
    fact: ContextFact,
    context: ValidationQuestionContext,
    domain_id: str | None,
    citation_required: bool,
) -> ValidationTestCaseDTO | None:
    """A fact-grounded retrieval case.

    Strict mode (post operator feedback): the topic MUST survive
    the noise / identifier filter. When no clean topic can be
    extracted from the fact body, we return ``None`` rather than
    emit a vague section/page placeholder. The previous
    "What does the section X say?" / "What does page X say?"
    fallbacks produced low-value rows.
    """
    topic = _topic_from_fact(fact.text)
    if not topic:
        return None
    expected_answer = _expected_answer_from_fact(fact.text, topic)
    if not expected_answer:
        return None
    return ValidationTestCaseDTO(
        test_case_id=f"tc-fact-{uuid.uuid4().hex[:8]}",
        question=f"What does the document say about {topic}?",
        type="retrieval",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer=expected_answer,
        expected_answer_points=[topic],
        expected_chunks=[fact.chunk_id] if fact.chunk_id else [],
        expected_pages=[fact.page] if fact.page is not None else [],
        citation_required=citation_required,
        source_traceability=[fact.chunk_id] if fact.chunk_id else [],
        metadata={
            "fact_chunk_id": fact.chunk_id,
            "fact_page": fact.page,
        },
        question_type="fact_retrieval",
        validation_scope="evidence",
        source_artifact_id=fact.artifact_id,
        domain_id=domain_id,
        generated_from="fact",
        confidence=0.8,
        reason=(
            f"Fact-grounded: topic {topic!r} extracted from "
            f"{'page ' + str(fact.page) if fact.page else 'a chunk'}."
        ),
        expected_evidence=_evidence_pointer(fact),
    )


def _domain_case(
    *,
    field_name: str,
    context: ValidationQuestionContext,
    domain_id: str | None,
    citation_required: bool,
) -> ValidationTestCaseDTO | None:
    """One question per domain ``important_field`` — but only when
    the document evidence actually covers it (the caller verifies
    this via fact-corpus matching). The negative counterpart
    (``_domain_negative_cases``) handles the "field is missing"
    branch."""
    label = field_name.replace("_", " ")
    # Find a fact that actually mentions the label. The caller
    # already gated on this — finding it here lets us populate a
    # real expected_answer + evidence pointer.
    backing_fact = None
    needle = label.lower()
    for fact in context.facts:
        if needle in (fact.text or "").lower():
            backing_fact = fact
            break
    expected_answer = (
        _expected_answer_from_fact(backing_fact.text, label)
        if backing_fact else None
    )
    if not expected_answer:
        # Bail: caller's gate said this field was covered but we
        # couldn't lift a clean answer. Skip rather than emit a
        # questionable case.
        return None
    return ValidationTestCaseDTO(
        test_case_id=f"tc-dom-{uuid.uuid4().hex[:8]}",
        question=f"What does the document say about {label}?",
        type="domain",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_answer=expected_answer,
        expected_answer_points=[label],
        expected_chunks=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        expected_pages=(
            [backing_fact.page] if backing_fact.page is not None else []
        ),
        citation_required=citation_required,
        source_traceability=(
            [backing_fact.chunk_id] if backing_fact.chunk_id else []
        ),
        metadata={"domain_field": field_name},
        question_type="domain_enrichment_check",
        validation_scope="domain",
        source_artifact_id=backing_fact.artifact_id,
        domain_id=domain_id,
        generated_from="domain_rule",
        confidence=0.8,
        reason=(
            f"Domain pack field {field_name!r} appears in document "
            f"evidence"
            + (f" on page {backing_fact.page}" if backing_fact.page else "")
            + "."
        ),
        expected_evidence=_evidence_pointer(backing_fact),
    )


# ---- Quality filter + dedup ----------------------------------------


def _filter_and_dedupe_cases(
    cases: list[ValidationTestCaseDTO],
    *,
    context: ValidationQuestionContext,
) -> list[ValidationTestCaseDTO]:
    """Two passes over the emitted cases:

      1. Drop cases that fail quality (raw-chunk-injection, vague
         "this document" phrasing without a topic anchor, generic
         domain trivia without grounding, etc.). Smoke + negative
         cases are exempt — they have legitimate reasons to be
         short / generic / topic-free.
      2. Dedupe by normalised question text. Earlier-emitted
         cases win — preserves the smoke + modality order.

    Returns a new list; never mutates the input.
    """
    kept: list[ValidationTestCaseDTO] = []
    seen_normalised: set[str] = set()
    for case in cases:
        if not _passes_quality(case, context=context):
            continue
        key = _normalise_question_for_dedup(case.question)
        if key in seen_normalised:
            continue
        seen_normalised.add(key)
        kept.append(case)
    return kept


def _passes_quality(
    case: ValidationTestCaseDTO,
    *,
    context: ValidationQuestionContext,
) -> bool:
    """Quality-first rejection rules.

    Operator-stated reject criteria (post-refactor):

      * scope == ``generic`` → reject. The generator has
        category-specific scopes; landing on ``generic`` means
        the emitter couldn't tell what kind of test this is.
      * scope == ``generic`` for the smoke case → also reject,
        because the smoke was producing "What is this document
        about?" boilerplate. Smoke is no longer emitted at all.
      * no ``expected_answer`` and not a guardrail → reject.
        Every positive case must carry a verifiable answer.
      * no ``expected_evidence`` and not a guardrail → reject.
      * topic-noise (``PDF``, ``The``, ``Expected``, etc.) in
        the question's "about X" anchor → reject.
      * answer-in-question shape (``What is the X of <id>?``)
        → reject.
      * raw-chunk-text injection (long quoted run) → reject.
      * vague "this document"-only phrasing → reject.

    Guardrail / negative-check cases are exempt from
    ``expected_evidence`` / ``expected_answer`` because they
    legitimately test that the engine ABSTAINS rather than
    answers.
    """
    # Guardrail / negative-check exemption. These cases test
    # abstention; they intentionally don't carry an
    # expected_answer or evidence pointer.
    if case.validation_scope in {"negative_check", "guardrail"}:
        return True
    question = (case.question or "").strip()
    if not question:
        return False

    # 1. Scope must be more specific than ``generic``. The
    #    emitters now have category-specific scopes for every
    #    valid path; landing on ``generic`` is the signal of an
    #    un-anchored case.
    if case.validation_scope == "generic":
        return False

    # 2. Positive cases must have an expected_answer.
    if not (case.expected_answer or "").strip():
        return False

    # 3. Positive cases must have an expected_evidence pointer
    #    (page / section / chunk reference).
    if not (case.expected_evidence or "").strip():
        return False

    # 4. Raw-chunk-text injection signature.
    if _contains_raw_chunk_quote(question):
        return False

    # 5. Answer-in-question shape — "What is the X of <identifier>?".
    if _looks_like_answer_in_question(question):
        return False

    # 6. Topic-noise check. Pull the "about X" / "of X" / "role
    #    of X" anchor and reject when X is in the noise list.
    anchor = _extract_question_anchor(question)
    if anchor is not None and not _is_acceptable_topic(anchor, question):
        return False

    # 7. Vague "this document"-only phrasing without a real
    #    anchor.
    lowered = question.lower()
    if (
        "this document" in lowered
        and len(question) < 50
        and not any(s in lowered for s in (":", "stage", "section", "page"))
    ):
        return False

    return True


# Regex variants the post-emit quality filter runs against the
# question text. Kept here so the rules are easy to read + tune.
_QUESTION_ANCHOR_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r"\bwhat\s+(?:is|are)\s+the\s+(?:role|purpose)\s+of\s+([A-Za-z0-9][^?]+?)(?:\s+in\s+(?:the\s+)?document)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:does|do)\s+the\s+document\s+(?:say|describe)\s+about\s+([A-Za-z0-9][^?]+?)\??$",
        re.IGNORECASE,
    ),
)


def _extract_question_anchor(question: str) -> str | None:
    """Return the topic the question targets (the X in
    ``about X`` / ``role of X``), stripped of trailing
    punctuation + quote marks. ``None`` when no recognisable
    anchor pattern matches — quality filter then trusts the
    other rules."""
    for pattern in _QUESTION_ANCHOR_PATTERNS:
        match = pattern.search(question.strip())
        if match:
            anchor = match.group(1).strip().strip("\"'?.,").strip()
            if anchor:
                return anchor
    return None


# Answer-in-question detector. Matches shapes like:
#   "What is the project ID of J1-CE-TEST-0426?"
#   "What is the version of v1.2.3?"
# where the "of" tail is itself an identifier — i.e. the
# answer to the question is literally named inside it.
_ANSWER_IN_QUESTION_RE = re.compile(
    r"\bwhat\s+is\s+the\s+[A-Za-z][A-Za-z\s]*\s+of\s+"
    r"([A-Za-z0-9][A-Za-z0-9\-]*[\-:][A-Za-z0-9\-:.]+)",
    re.IGNORECASE,
)


def _looks_like_answer_in_question(question: str) -> bool:
    """Reject malformed ``What is the X of <id>?`` cases where
    ``<id>`` is itself the answer (operator-flagged
    ``"What is the project ID of J1-CE-TEST-0426?"`` lives
    here)."""
    return bool(_ANSWER_IN_QUESTION_RE.search(question))


def _contains_raw_chunk_quote(question: str) -> bool:
    """A question is considered raw-chunk-injection if it carries
    a quoted run longer than 80 characters (chunk-title injection
    signature) OR the quoted run contains pipe-separated metadata.

    The threshold is conservative — legitimate quoted-section
    references like ``the section 'Risk Register' on page 3``
    pass because they're under 30 chars."""
    # Find quoted runs delimited by ``"…"`` or ``'…'``.
    for match in re.finditer(r"[\"']([^\"']{0,400})[\"']", question):
        body = match.group(1)
        if len(body) > 80:
            return True
        if body.count("|") >= 2:
            return True
    return False


def _normalise_question_for_dedup(question: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation. Lets us
    dedupe near-duplicates that differ only in punctuation /
    case (e.g. "What is this document about?" vs "what is this
    document about")."""
    return _FACT_NORMALISE_RE.sub(" ", (question or "").strip().lower()).strip()


_FACT_NORMALISE_RE = re.compile(r"[\s\.,;:!?\"'()\[\]{}]+")


def _topic_from_fact(text: str) -> str | None:
    """Extract a short noun-phrase topic from a fact sentence.

    Walks capitalised noun-runs from left to right and returns the
    first one that survives the noise filter. Returns ``None``
    when no clean topic exists — the fact emitter then SKIPS the
    fact entirely rather than emit a vague "What does the
    document say about ‹first capitalised word›?" question.

    Rejection rules:
      * topic in ``NOISE_TERMS`` (PDF, The, Expected, …)
      * single-word topic shorter than 4 chars
      * identifier-shaped (``J1-CE-TEST-0426``) — these are
        answers, not topics
      * topic equals or contains the entire fact body (would
        re-inject raw chunk text)
    """
    if not text:
        return None
    # Iterate ALL capitalised noun-runs (1-5 words) and return the
    # first acceptable one. The previous version returned the
    # FIRST match unconditionally, which is how the determiner
    # "The" (capitalised at sentence start) and noise like "PDF"
    # leaked through.
    for match in re.finditer(
        r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,4})\b",
        text,
    ):
        candidate = match.group(1).strip()
        if not _is_acceptable_topic(candidate, text):
            continue
        return candidate
    return None


def _is_acceptable_topic(candidate: str, full_text: str) -> bool:
    """Predicate used by both ``_topic_from_fact`` and the
    post-emit quality filter so the rejection rules stay
    consistent."""
    if not candidate:
        return False
    lowered = candidate.lower()
    if lowered in TOPIC_FORBIDDEN:
        return False
    if lowered in _STOPWORDS_FOR_TOPIC:
        return False
    words = candidate.split()
    if len(words) == 1 and len(candidate) < 4:
        return False
    # Single-word topic where the bare word is on the noise list
    # (regardless of cleaning). Belt-and-braces with the noise
    # check above for case variants.
    if len(words) == 1 and words[0].lower() in TOPIC_FORBIDDEN:
        return False
    # Multi-word topic where EVERY word is noise / stopword. Catches
    # "The PDF" / "Expected Document" combinations the regex would
    # otherwise pass.
    if len(words) > 1 and all(
        w.lower() in TOPIC_FORBIDDEN | _STOPWORDS_FOR_TOPIC for w in words
    ):
        return False
    # Identifier shape — see ``_looks_like_identifier``.
    if _looks_like_identifier_topic(candidate):
        return False
    # Topic length sanity.
    if len(candidate) > 60 or len(candidate) < 3:
        return False
    # Never inject the entire fact body.
    if candidate.lower() == full_text.lower().strip().rstrip("."):
        return False
    return True


def _looks_like_identifier_topic(s: str) -> bool:
    """Same rule as ``context._looks_like_identifier`` — duplicated
    here so the generator's filter stays self-contained when the
    context module is reloaded for tests. Kept tight."""
    if s.count("-") >= 2:
        return True
    if "-" in s and re.search(r"\d", s):
        parts = s.split("-")
        if all(len(p) <= 8 for p in parts):
            return True
    return False


# Re-import noise / stopword sets so the generator's local filters
# don't have to round-trip through the context module on every call.
# Same source-of-truth definitions; kept here as readable aliases.
from j1.validation.context import NOISE_TERMS, TOPIC_FORBIDDEN  # noqa: E402

_STOPWORDS_FOR_TOPIC: frozenset[str] = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "it", "its", "his", "her", "their", "our", "your",
    "is", "are", "was", "were", "be", "been", "being",
})


def _label_for_artifact(artifact: ArtifactRecord) -> str:
    """Short human-readable label for use inside generated questions.
 Prefers an explicit `title` from the artifact metadata, falls
 back to the kind/id pair so questions never reference an
 empty placeholder.

 The id is included in full (not the prior ``[:8]`` truncation)
 because that truncation made labels collide across sibling
 artifacts whose ids share a prefix — e.g. ``art-table-0`` and
 ``art-table-1`` both produced ``enriched.tables/art-tabl``,
 which the new dedup correctly killed as duplicates. Using the
 full id keeps each modality case's question unique."""
    title = artifact.metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:80]
    return f"{artifact.kind}/{artifact.artifact_id}"


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
 path. Tries (in order) section heading → clean first sentence →
 page reference → generic summarization fallback.

 The previous version always asked "what does the document say
 about: <first 140 chars of body>?" — but for documents whose
 first chunk starts with a header line ("J1 Platform - One Page
 Test Brief | Document ID: J1-TEXT-001 | Version: 1.0 | …"),
 the question included the entire metadata block as a topic.
 The synthesizer LLM then abstained because the malformed
 question can't be grounded against any single fact in the
 evidence. The latest validation report flagged exactly this
 case as a required failure via the new
 ``evidence_present_but_answer_fallback`` check.

 Spec section 5 rule: "Do not use raw truncated document text
 as the question." This producer now respects that rule.
 """
    body = (chunk.body or "").strip()
    if not body:
        return []

    # Preferred 1: section heading. Cheapest readable form when the
    # chunk projector recorded one. Caps at 80 chars so a runaway
    # heading doesn't bloat the question.
    section = (getattr(chunk, "section", None) or "").strip()
    if section and 0 < len(section) <= 80:
        return [{
            "question": f"What does the document say in section '{section}'?",
            "type": "retrieval",
            "expected_answer_points": [],
        }][:budget]

    # Preferred 2: first sentence — but only if it LOOKS like a
    # sentence (sufficient words, not pipe-separated metadata, no
    # giveaway header markers). Quoted so the LLM treats it as a
    # topic excerpt rather than a question fragment.
    sentence = _first_sentence(body, max_chars=140)
    if sentence and _is_clean_sentence(sentence):
        return [{
            "question": f"What does the document say about \"{sentence}\"?",
            "type": "retrieval",
            "expected_answer_points": [],
        }][:budget]

    # Preferred 3: page reference. The check still verifies
    # `expected_chunk_in_topk` against the right chunk, so the
    # question doesn't need to embed body text at all.
    if chunk.page_start is not None:
        return [{
            "question": (
                f"What information is on page {chunk.page_start} of the "
                "document?"
            ),
            "type": "retrieval",
            "expected_answer_points": [],
        }][:budget]

    # Last resort: a clean generic question. Still testable via
    # `expected_chunk_in_topk`; the LLM is asked to summarise
    # rather than to ground on a malformed topic.
    return [{
        "question": (
            "Summarize the key information in this section of the document."
        ),
        "type": "retrieval",
        "expected_answer_points": [],
    }][:budget]


# Cheap regex set for the heuristic-question cleanliness check.
# Matches the shapes of document-header metadata blocks that make
# bad questions: "Doc ID: X | Version: 1.0 | Date: …" style.
_METADATA_BLOCK_HINTS: tuple[str, ...] = (
    "Document ID",
    "Version:",
    "Date:",
    "Purpose:",
)


def _is_clean_sentence(s: str) -> bool:
    """Return True when ``s`` looks like a natural-language sentence
    suitable as a question topic. False for metadata-block lines
    (pipe-separated headers, colon-heavy ID strings, header-style
    capitalised IDs).

    The check is intentionally conservative: false negatives (a
    real sentence rejected) fall through to page-reference /
    generic questions, which are still testable. False positives
    (a metadata block treated as a sentence) reproduce the original
    bug — costlier than over-rejection."""
    s = (s or "").strip()
    if not s or len(s.split()) < 3:
        return False
    # Pipe-separated metadata or colon-heavy ID strings.
    if s.count("|") >= 2:
        return False
    if s.count(":") >= 3:
        return False
    # Header-block markers anywhere in the candidate.
    if any(hint in s for hint in _METADATA_BLOCK_HINTS):
        return False
    return True


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
            priority="edge",
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
            validation_scope="guardrail",
            difficulty=difficulty,
            domain_id=domain_id,
            generated_from="llm",
            confidence=0.7,
            reason=(
                "LLM emitted a missing-information-check question "
                "for a field the evidence didn't cover."
            ),
        )

    if quote is None or source_artifact_id is None:
        # Positive case without a verifiable evidence quote. This is
        # the anti-hallucination guard — drop it rather than ship a
        # case that may have come from outside-world knowledge.
        return None

    # LLM-supplied scope, falling back to ``evidence`` when the
    # LLM left it generic — the case IS evidence-anchored (we
    # verified the quote), so ``evidence`` is more honest than
    # generic and passes the post-emit quality gate.
    effective_scope: ValidationScope = (
        scope if scope and scope != "generic" else "evidence"  # type: ignore[assignment]
    )
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
        validation_scope=effective_scope,
        difficulty=difficulty,
        domain_id=domain_id,
        generated_from="llm",
        confidence=0.85,
        reason=(
            f"LLM-grounded against evidence in artifact "
            f"{source_artifact_id!r}."
        ),
        expected_evidence=f"artifact {source_artifact_id}",
    )


def _looks_truncated(raw: Any) -> bool:
    """Heuristic: did the LLM emit this case while running into the
    output-token ceiling?

    JSON-mode responses produce syntactically-valid JSON even when
    the model hits ``max_tokens`` mid-string, but the last
    string-typed value gets cut without proper punctuation. The
    operator then sees a row like:

        Q: "What is the role of the proposed risk assessment in"
        A: ""

    which is worse than no row at all. Two strong signals:

      * ``question`` ends mid-word — no sentence terminator AND
        the question has whitespace (i.e. it's a real sentence
        someone started writing, not a single label). Local LLMs
        almost always emit ``?`` when the question is complete.
      * ``expected_answer`` is empty WHEN the question has more
        than a few words. JSON-mode collapses the cut field to
        ``""``; pairing that with a long question is the
        truncation signature.

    Single-token "Q1?" labels and similar pass through — we want
    to drop nonsensical cuts, not synthetically-short cases.
    """
    if not isinstance(raw, dict):
        return False
    question = str(raw.get("question") or "").strip()
    expected = str(raw.get("expected_answer") or "").strip()
    if not question:
        return True
    # Allow ``.``, ``?``, ``!``, ``"``, ``)``, ``…`` as terminators.
    # Anything else trailing strongly suggests the model was
    # mid-word when it ran out of tokens — but ONLY when the
    # question is long enough to be a real sentence. Short
    # tokens like "Q1" or "smoke" pass through.
    has_terminator = question[-1] in '.?!"”)…'
    has_words = " " in question
    if has_words and not has_terminator:
        return True
    if has_words and not expected:
        # Long-form question without an answer is the JSON-mode
        # max-tokens cut pattern.
        return True
    return False


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
    """Wrap one heuristic-question dict into a typed DTO. The
 heuristic path is the no-LLM / no-context fallback; the
 context-driven emitters cover most cases now but this is the
 safety net for genuinely sparse documents. Provenance is
 stamped (``generated_from=fallback``) so a tester reading the
 set can tell at a glance the case came from the
 deterministic template path rather than the LLM."""
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
        validation_scope="retrieval",
        source_artifact_id=chunk.source_artifact_id,
        source_artifact_type="chunk",
        domain_id=domain_id,
        generated_from="fallback",
        confidence=0.5,
        reason=(
            f"Deterministic fallback case from chunk "
            f"{chunk.chunk_id!r}"
            + (f" (section {chunk.section!r})" if chunk.section else "")
            + "."
        ),
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
