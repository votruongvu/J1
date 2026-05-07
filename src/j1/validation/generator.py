"""Generate validation test cases from an ingested document's chunks.

Phase 2 ships a single concrete implementation: `DefaultTestCaseGenerator`.
It samples chunks from the run, asks the configured FAST/text LLM to
propose 1–2 retrieval-style questions per chunk, and emits a
`ValidationSetDTO` ready for the runner.

Design rules:

  * **Generated, not gold.** The output is labelled `source="generated"`
    and `status="draft"`. Tester edits / approval workflows live in
    Phase 5; the field exists today so the wire shape doesn't churn.
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation.dtos import (
    ValidationSetDTO,
    ValidationTestCaseDTO,
)

_log = logging.getLogger("j1.validation.generator")

# Generator version — bump when the case shape / sampling rules / prompt
# change in a way that should invalidate cached sets. Persisted on the
# `ValidationSetDTO`, used by callers as part of the idempotency key.
GENERATOR_VERSION = "v1"

# Hard caps. Match the Phase 2 plan: synchronous in-process execution
# only, total ≤ 50 cases. Sampling stays well under so the LLM round
# trip count is bounded.
_MAX_CHUNK_SAMPLES = 8
_MAX_QUESTIONS_PER_CHUNK = 2
_PREVIEW_MAX_CHARS = 240

# Negative-case pool. Off-topic prompts the document is extremely
# unlikely to answer, by design. Deterministic — no LLM call needed,
# no domain inference; the runner's check is whether the engine
# abstains, which doesn't require a domain-specific question. Phase
# 4+ may layer in an LLM-driven domain-aware variant; for Phase 3
# this fixed pool ships every set with reproducible negatives.
#
# The list is intentionally diverse so retrievers can't accidentally
# match a single keyword (e.g. "price"). One question per category
# (finance, sports, recipes, celebrity gossip, geography); the runner
# samples up to 2 by default.
_NEGATIVE_PROMPTS: tuple[str, ...] = (
    "What is the current price of Bitcoin?",
    "Who won the most recent FIFA World Cup?",
    "What's a good recipe for chocolate cake?",
    "What is the capital city of the planet Mars?",
    "Who is currently the highest-paid celebrity in Hollywood?",
)
_DEFAULT_NEGATIVE_COUNT = 2

# Schema for the LLM's structured-extract response. The text client
# enforces JSON-schema-style structured outputs (per the LM Studio fix
# we shipped earlier), so the model is constrained to this shape.
_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "type": {"type": "string", "enum": ["retrieval", "answer"]},
                    "expected_answer_points": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["question", "type"],
            },
        },
    },
    "required": ["questions"],
}


_DEFAULT_PROMPT = (
    "You are generating test questions for a knowledge-base validation "
    "harness. Given a document chunk, propose 1 to 2 short, "
    "self-contained questions a tester could ask to verify the chunk's "
    "content is retrievable from the index. Each question must be "
    "answerable from the chunk alone. For each question, label its "
    "type as 'retrieval' (the question asserts a specific fact "
    "appears in the document) or 'answer' (the question expects a "
    "synthesized answer). Optionally list 1-2 short answer points.\n"
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
    # Phase 3: count of off-topic negative cases to include. The
    # generator picks them deterministically from `_NEGATIVE_PROMPTS`
    # so the same set is reproducible across regenerations. Zero
    # disables negatives entirely (e.g. for very small case budgets).
    negative_case_count: int = _DEFAULT_NEGATIVE_COUNT
    # Phase 4: per-modality cap on emitted cases. The generator
    # samples up to N table / N image / N graph cases (where N
    # below) to keep the total bounded. Zero disables a modality
    # entirely; high values are still subject to the global
    # `max_cases` ceiling.
    max_table_cases: int = 3
    max_image_cases: int = 3
    max_graph_cases: int = 3


# ---- Generator -----------------------------------------------------


class DefaultTestCaseGenerator:
    """Phase 2's single implementation.

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
        self._prompt = prompt or _DEFAULT_PROMPT

    def generate(
        self,
        *,
        run_id: str,
        document_ids: list[str],
        chunks: list[_ChunkRecord],
        options: GenerationOptions | None = None,
        actor: str | None = None,
        # Phase 4: optional modality artifacts. Each list is the
        # subset of the run's artifacts of the given kind. The
        # service builds these from the registry; the generator
        # treats empty lists as "no modality cases to emit."
        table_artifacts: list[ArtifactRecord] | None = None,
        visual_artifacts: list[ArtifactRecord] | None = None,
        graph_artifacts: list[ArtifactRecord] | None = None,
    ) -> ValidationSetDTO:
        """Build a validation set from the run's chunks + modality
        artifacts.

        Always returns a set, even when `chunks` is empty — in that
        case only the smoke + negatives are emitted, which the
        runner will exercise as smoke (engine returns nothing →
        retrieved_chunks_present fails). That's the right signal
        for "the run produced nothing queryable."

        Phase 4 modalities ship in fixed order: smoke → negatives →
        tables → images → graph → chunk cases. This keeps the
        sequencing reproducible across regenerations.
        """
        opts = options or GenerationOptions()
        sampled = _sample_chunks(
            chunks,
            max_samples=_MAX_CHUNK_SAMPLES,
            must_include_ids=opts.must_include_chunk_ids,
        )

        cases: list[ValidationTestCaseDTO] = [
            _smoke_case(citation_required=opts.citation_required),
        ]

        # Phase 3: deterministic negatives drawn from a fixed pool.
        # Inserted right after the smoke case so they show up early
        # in execution order (smoke priority is highest; negatives
        # are normal priority but appear before chunk cases for
        # operator readability).
        negative_count = max(
            0, min(opts.negative_case_count, len(_NEGATIVE_PROMPTS)),
        )
        for prompt in _NEGATIVE_PROMPTS[:negative_count]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_negative_case(prompt))

        # Phase 4 modality cases. Each modality's count is bounded
        # by both `opts.max_*_cases` (per-modality) and the global
        # `opts.max_cases` ceiling. We emit tables → images → graph
        # in that fixed order so two runs over the same artifacts
        # produce identical case sequences.
        for table in (table_artifacts or [])[:opts.max_table_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_table_case(table))
        for visual in (visual_artifacts or [])[:opts.max_image_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_image_case(visual))
        for graph in (graph_artifacts or [])[:opts.max_graph_cases]:
            if len(cases) >= opts.max_cases:
                break
            cases.append(_graph_case(graph))

        # Cap chunk-derived cases so the total respects max_cases.
        remaining_budget = max(0, opts.max_cases - len(cases))
        per_chunk_budget = (
            min(_MAX_QUESTIONS_PER_CHUNK, max(1, remaining_budget // max(1, len(sampled))))
            if sampled else 0
        )

        for chunk in sampled:
            if len(cases) >= opts.max_cases:
                break
            slot = min(per_chunk_budget, opts.max_cases - len(cases))
            generated = self._cases_for_chunk(
                chunk,
                budget=slot,
                citation_required=opts.citation_required,
            )
            cases.extend(generated)

        artifacts_hash = _hash_chunks(sampled)
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
                # Phase 4 surfaces — let the FE/REST layer answer
                # "did the generator look at modalities?" without
                # scanning every emitted case.
                "table_artifact_count": len(table_artifacts or []),
                "visual_artifact_count": len(visual_artifacts or []),
                "graph_artifact_count": len(graph_artifacts or []),
            },
        )

    # ---- Internals -----------------------------------------------------

    def _cases_for_chunk(
        self,
        chunk: _ChunkRecord,
        *,
        budget: int,
        citation_required: bool,
    ) -> list[ValidationTestCaseDTO]:
        """Try the LLM first; fall back to the deterministic heuristic
        on any failure. Failures are logged at debug — the operator
        sees the fallback's questions, which is preferable to an
        error and an empty set."""
        if budget <= 0:
            return []
        questions = self._llm_questions_for_chunk(chunk, budget=budget)
        if not questions:
            questions = _heuristic_questions_for_chunk(chunk, budget=budget)
        cases: list[ValidationTestCaseDTO] = []
        for q in questions[:budget]:
            cases.append(
                ValidationTestCaseDTO(
                    test_case_id=f"tc-{uuid.uuid4().hex[:10]}",
                    question=q["question"],
                    type=q.get("type", "retrieval"),
                    priority="normal",
                    expected_behavior="answer_with_citations",
                    expected_answer_points=list(q.get("expected_answer_points") or []),
                    expected_chunks=[chunk.chunk_id] if chunk.chunk_id else [],
                    expected_pages=_pages_for_chunk(chunk),
                    citation_required=citation_required,
                    source_traceability=[chunk.chunk_id] if chunk.chunk_id else [],
                    metadata={
                        "section": chunk.section,
                        "title": chunk.title,
                    },
                )
            )
        return cases

    def _llm_questions_for_chunk(
        self, chunk: _ChunkRecord, *, budget: int,
    ) -> list[dict[str, Any]]:
        """Best-effort LLM call. Returns [] on any failure so the
        caller's fallback path runs."""
        if self._text_client is None:
            return []
        body = (chunk.body or "").strip()
        if not body:
            return []
        # Truncate the chunk body so we don't send 10k tokens of
        # context per case to a small local LLM. 4000 chars is a
        # reasonable cap for FAST-role models.
        excerpt = body[:4000]
        full_prompt = (
            f"{self._prompt}\nGenerate at most {budget} questions.\n\n"
            f"Chunk content:\n---\n{excerpt}\n---"
        )
        try:
            parsed, _usage = self._text_client.extract(
                full_prompt, _QUESTION_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 — fall back rather than fail
            _log.debug(
                "LLM question generation failed for chunk %r: %s",
                chunk.chunk_id, exc,
            )
            return []
        questions = parsed.get("questions") if isinstance(parsed, dict) else None
        if not isinstance(questions, list):
            return []
        # Defensive: drop entries that don't carry a usable question.
        cleaned: list[dict[str, Any]] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question") or "").strip()
            if not text:
                continue
            cleaned.append({
                "question": text,
                "type": (
                    q.get("type") if q.get("type") in ("retrieval", "answer")
                    else "retrieval"
                ),
                "expected_answer_points": [
                    str(p) for p in (q.get("expected_answer_points") or [])
                    if isinstance(p, str)
                ],
            })
        return cleaned


# ---- Module-level helpers (unit-testable in isolation) -------------


def _smoke_case(*, citation_required: bool) -> ValidationTestCaseDTO:
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
    )


def _negative_case(question: str) -> ValidationTestCaseDTO:
    """Phase 3: an off-topic test case. The runner expects the
    engine to abstain (regex check) and not fabricate (judge check).
    `expected_chunks` / `expected_answer_points` are intentionally
    empty — there's nothing in the document to cite."""
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
        metadata={"negative": True},
    )


# ---- Phase 4: modality case factories ------------------------------
#
# Each factory builds a case asserting that the named artifact is
# retrievable for a topic-related question. The question text is
# deterministic — derived from the artifact's title or kind — so
# regenerating produces identical case sequences. LLM-driven
# question phrasing (with chunk content) is a Phase 4+ enhancement.


def _table_case(artifact: ArtifactRecord) -> ValidationTestCaseDTO:
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
    )


def _image_case(artifact: ArtifactRecord) -> ValidationTestCaseDTO:
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
    )


def _graph_case(artifact: ArtifactRecord) -> ValidationTestCaseDTO:
    """A retrieval check for graph snapshots.

    Phase 4 keeps this lightweight: we ask the engine to surface
    the document's main entities/relationships. The check verifies
    that retrieval engaged the graph artifact at all (Phase 5 may
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

      1. Always include the explicit `must_include_ids` (Phase 5 will
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
    Phase 2 contract."""
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
