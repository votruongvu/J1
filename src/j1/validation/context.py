"""Question-context builder for the validation generator.

The previous generator emitted hardcoded boilerplate (``"What is
this document about?"``, ``"What does the document say about
'<raw chunk title>'?"``) because it only saw chunks and had no
notion of WHAT the document was actually about. This module
gathers the structured signals from every source the registry
makes available — chunks, the enriched document map, the
enrichment summary, the final ingestion report, and the domain
pack — into a single dataclass the generators can read.

The context is intentionally **decoupled from the question text**.
It records FACTS, ENTITIES, SECTIONS, etc. The generators then
phrase questions over those facts. That separation is what stops
the generator from injecting raw chunk-body fragments into the
question string (the spec's section-3 rule).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from j1.artifacts.registry import ArtifactRecord
from j1.domains.models import DomainValidationGuidance
from j1.ingestion_review.projectors.chunks import _ChunkRecord


# Section/heading lines worth using as workflow seeds. We require
# them to look like actual document headings (short, no terminal
# punctuation, no leading bullet). Anything beyond ~80 chars is
# almost certainly a paragraph that happens to live at the top of
# a chunk.
_SECTION_MAX_CHARS = 80


# Words / phrases we strip from a "key fact" candidate before
# deduping. Stops "Stage 1" / "stage one" from looking distinct
# from "Stage 1." after normalisation.
_FACT_NORMALISE_RE = re.compile(r"[\s\.,;:!?\"'()\[\]{}]+")


# Stopwords used by the entity / fact extractors. Tiny, no NLP
# library — we don't need precision, just enough to skip the
# "the / a / and / of" noise when surfacing candidate entities.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "of",
        "to", "in", "for", "with", "on", "at", "by", "as", "is",
        "are", "was", "were", "be", "been", "being", "this", "that",
        "these", "those", "from", "into", "about", "over", "under",
        "between", "through", "during", "before", "after", "above",
        "below", "until", "since", "while", "without", "within",
        "across", "against", "along", "around", "behind", "beside",
        "beyond", "near", "off", "out", "outside", "past", "throughout",
        "toward", "towards", "upon", "via",
    }
)


# Question-noise stop-list. These are words that get incidentally
# capitalised in technical documents (file types, structural
# pointers, generic UI labels) but make terrible question targets.
# Operators flagged questions like:
#   "What is the role of PDF in the document?"
#   "What is the role of Expected in the document?"
#   "What does the document say about The?"
# Every one came from this set leaking through the entity / topic
# extractor. We reject these as candidate entities AND as question
# topics — they're never specific enough to anchor a useful test.
NOISE_TERMS: frozenset[str] = frozenset(
    {
        # File types / common artefact words.
        "pdf", "doc", "docx", "txt", "csv", "json", "xml", "html",
        "yaml", "yml", "markdown", "md", "rst",
        # Structural pointers.
        "document", "page", "section", "appendix", "table",
        "image", "figure", "exhibit", "chapter", "header",
        "footer", "footnote",
        # Validation / testing terminology.
        "test", "tests", "testing", "validation", "validations",
        "question", "questions", "answer", "answers", "expected",
        "actual", "verify", "verified",
        # Generic pronouns / determiners (capitalised at
        # sentence start they slip past the stop-words).
        "the", "a", "an", "this", "that", "these", "those",
        "it", "its", "his", "her", "their", "our", "your",
        "some", "any", "all", "each", "every",
        # Common short tokens that look like entities but
        # carry no semantic anchor.
        "id", "no", "ref", "fig", "vol", "pp", "para",
        "yes", "ok", "n/a", "tbd",
    }
)


# Words that must NEVER appear as the bare topic of a question.
# Stricter than ``NOISE_TERMS`` because some legitimate entities
# could in theory survive the noise check (e.g. "Page 7" — page is
# noise but pairing with a number is not). Used by the topic
# extractor and the post-emit quality filter.
TOPIC_FORBIDDEN: frozenset[str] = NOISE_TERMS


# Entity-candidate regex: a run of capitalised words (optionally
# followed by a numeric suffix like ``Stage 1``, ``DOT-2024``),
# optionally joined by ``and`` / ``of`` / ``&`` / ``-``. Catches
# multi-word proper nouns + numbered identifiers without an NLP
# library. Conservative — false negatives are better than emitting
# hallucinated "entities" from prose.
_CAP_WORD = r"[A-Z][A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
_CAP_OR_NUM = rf"(?:{_CAP_WORD}|\d+)"
_ENTITY_CANDIDATE_RE = re.compile(
    rf"\b{_CAP_WORD}(?:\s+{_CAP_OR_NUM})*\b"
)


# Sentence-splitter. Cheap and good enough for short chunks; we
# don't want a sentence-tokeniser dependency for this.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.!?])\s+(?=[A-Z0-9])")


@dataclass(frozen=True)
class ContextFact:
    """One atomic fact extracted from the document.

    A fact is a single sentence (under 200 chars) lifted verbatim
    from chunk body text. We pair it with the chunk it came from
    so the question generator can stamp evidence pointers.
    """

    text: str
    chunk_id: str | None = None
    artifact_id: str | None = None
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class ContextEntity:
    """One named entity the chunks/graph mention.

    ``occurrences`` is the count across the inspected text — used
    by the generator to pick the highest-signal entities first.
    """

    name: str
    occurrences: int = 1
    source: str = "chunk"  # ``"chunk"`` | ``"graph"`` | ``"document_map"``


@dataclass(frozen=True)
class ContextSection:
    """One document section / heading + its primary page (when known)."""

    title: str
    page: int | None = None


@dataclass(frozen=True)
class ValidationQuestionContext:
    """Structured snapshot of the document used for question
    generation.

    Every field is OPTIONAL — the builder gracefully degrades when
    only chunks (or only the final report) is available. The
    generators tolerate empty lists by emitting fewer cases
    rather than falling back to generic boilerplate.
    """

    document_title: str | None = None
    document_id: str | None = None
    document_purpose: str | None = None
    domain_id: str | None = None
    domain_guidance: DomainValidationGuidance | None = None
    facts: tuple[ContextFact, ...] = ()
    entities: tuple[ContextEntity, ...] = ()
    relationships: tuple[str, ...] = ()
    sections: tuple[ContextSection, ...] = ()
    workflow_stages: tuple[str, ...] = ()
    page_count: int | None = None
    has_tables: bool = False
    has_visuals: bool = False
    has_graph: bool = False

    def has_any_facts(self) -> bool:
        """Whether the context carries enough material to author
        document-specific questions. When False, the generator
        emits the conservative no-context output (smoke only +
        any domain-driven negatives)."""
        return bool(self.facts) or bool(self.entities) or bool(self.sections)


def build_question_context(
    *,
    chunks: list[_ChunkRecord],
    table_artifacts: list[ArtifactRecord] | None = None,
    visual_artifacts: list[ArtifactRecord] | None = None,
    graph_artifacts: list[ArtifactRecord] | None = None,
    enriched_artifacts: list[ArtifactRecord] | None = None,
    final_report: dict[str, Any] | None = None,
    domain_id: str | None = None,
    domain_guidance: DomainValidationGuidance | None = None,
) -> ValidationQuestionContext:
    """Assemble a ``ValidationQuestionContext`` from whatever the
    registry exposed for the run.

    Order of precedence:
      1. ``final_report`` — highest-trust metadata (title, doc id,
         page_count). Pulled from ``final_ingestion_report.json``.
      2. ``enriched_artifacts`` — when ``enriched.document_map``
         is registered, mine it for entities / sections /
         relationships.
      3. ``chunks`` — always. Provides facts (first clean sentence
         per chunk), entity candidates (capitalised-noun-runs),
         and section headings.

    The builder never raises on malformed inputs — it just
    contributes what it can extract.
    """
    document_title = _extract_document_title(
        final_report=final_report,
        chunks=chunks,
    )
    document_id = _extract_document_id(
        final_report=final_report,
        chunks=chunks,
    )
    document_purpose = _extract_document_purpose(
        final_report=final_report,
        enriched_artifacts=enriched_artifacts or [],
    )
    facts = _extract_facts_from_chunks(chunks)
    entities = _extract_entities(
        chunks=chunks,
        graph_artifacts=graph_artifacts or [],
        enriched_artifacts=enriched_artifacts or [],
    )
    sections = _extract_sections(chunks)
    workflow_stages = _extract_workflow_stages(chunks, entities)
    relationships = _extract_relationships(
        enriched_artifacts=enriched_artifacts or [],
        graph_artifacts=graph_artifacts or [],
    )
    page_count = _extract_page_count(
        final_report=final_report, chunks=chunks,
    )

    return ValidationQuestionContext(
        document_title=document_title,
        document_id=document_id,
        document_purpose=document_purpose,
        domain_id=domain_id,
        domain_guidance=domain_guidance,
        facts=facts,
        entities=entities,
        relationships=relationships,
        sections=sections,
        workflow_stages=workflow_stages,
        page_count=page_count,
        has_tables=bool(table_artifacts),
        has_visuals=bool(visual_artifacts),
        has_graph=bool(graph_artifacts),
    )


# ---- Extraction helpers ---------------------------------------------


def _extract_document_title(
    *,
    final_report: dict[str, Any] | None,
    chunks: list[_ChunkRecord],
) -> str | None:
    """Document title preference: final_report → first chunk title."""
    if final_report:
        name = final_report.get("document_name")
        if isinstance(name, str) and name.strip():
            return _clean_title(name)
    for chunk in chunks:
        title = (chunk.title or "").strip()
        if title and len(title) <= 120:
            return _clean_title(title)
    return None


def _extract_document_id(
    *,
    final_report: dict[str, Any] | None,
    chunks: list[_ChunkRecord],
) -> str | None:
    if final_report:
        did = final_report.get("document_id")
        if isinstance(did, str) and did.strip():
            return did.strip()
    for chunk in chunks:
        # ``_ChunkRecord`` doesn't carry document_id directly but
        # ``source_artifact_id`` is a usable fallback for FE
        # display when nothing better is available.
        if chunk.source_artifact_id:
            return None  # not the real doc id; better to surface ``None``
    return None


def _extract_document_purpose(
    *,
    final_report: dict[str, Any] | None,
    enriched_artifacts: list[ArtifactRecord],
) -> str | None:
    """Look for a one-line "purpose" or "summary" hint.

    Sources, in order:
      * ``final_report["compile_summary"]["quality_verdict"]`` —
        not a purpose but a useful one-liner when nothing better.
      * Any enriched artifact whose name starts with
        ``enriched.summary`` (when registered). The artifact's
        ``metadata`` dict carries the summary text.
    """
    if final_report:
        cs = final_report.get("compile_summary") or {}
        verdict = cs.get("quality_verdict")
        if isinstance(verdict, str) and verdict.strip():
            return verdict.strip()
    for art in enriched_artifacts:
        if art.kind == "enriched.summary":
            summary_text = (
                (art.metadata or {}).get("summary")
                or (art.metadata or {}).get("text")
            )
            if isinstance(summary_text, str) and summary_text.strip():
                # Cap at 240 chars — this gets embedded in prompts.
                return summary_text.strip()[:240]
    return None


def _extract_facts_from_chunks(
    chunks: list[_ChunkRecord],
    *,
    max_facts: int = 30,
) -> tuple[ContextFact, ...]:
    """Pull the first 1–2 clean sentences from each chunk body.

    "Clean" = not a metadata block (Document ID: X | Version: Y |
    …), not too short, not too long. Operators in spec-section 3
    explicitly call out the previous behaviour (raw-chunk-title
    injection) as the bug to fix; the cleanliness filter here is
    what stops a re-occurrence.
    """
    out: list[ContextFact] = []
    seen_normalised: set[str] = set()
    for chunk in chunks:
        body = (chunk.body or "").strip()
        if not body:
            continue
        for sentence in _split_sentences(body, max_sentences=2):
            if not _is_useful_sentence(sentence):
                continue
            key = _normalise_for_dedup(sentence)
            if key in seen_normalised:
                continue
            seen_normalised.add(key)
            out.append(
                ContextFact(
                    text=sentence,
                    chunk_id=chunk.chunk_id,
                    artifact_id=chunk.source_artifact_id,
                    page=chunk.page_start,
                    section=chunk.section,
                )
            )
            if len(out) >= max_facts:
                return tuple(out)
    return tuple(out)


def _extract_entities(
    *,
    chunks: list[_ChunkRecord],
    graph_artifacts: list[ArtifactRecord],
    enriched_artifacts: list[ArtifactRecord],
    max_entities: int = 20,
) -> tuple[ContextEntity, ...]:
    """Collect candidate entities from three sources.

    Graph and document_map entities (when present) carry the
    highest signal — they're the ones the indexer already
    promoted. Chunk-derived entities are best-effort capitalised-
    noun-run candidates.
    """
    counts: dict[str, int] = {}
    sources: dict[str, str] = {}

    # 1. Graph artifacts: top_entities in metadata.
    for art in graph_artifacts:
        for name in _iter_graph_top_entities(art):
            cleaned = _clean_entity_name(name)
            if not cleaned:
                continue
            counts[cleaned] = counts.get(cleaned, 0) + 5  # weight
            sources.setdefault(cleaned, "graph")

    # 2. enriched.document_map: when present, walk its
    #    ``entities`` list.
    for art in enriched_artifacts:
        if art.kind != "enriched.document_map":
            continue
        for name in _iter_document_map_entities(art):
            cleaned = _clean_entity_name(name)
            if not cleaned:
                continue
            counts[cleaned] = counts.get(cleaned, 0) + 3
            sources.setdefault(cleaned, "document_map")

    # 3. Chunk text — capitalised noun runs.
    for chunk in chunks:
        body = (chunk.body or "")
        if not body:
            continue
        for match in _ENTITY_CANDIDATE_RE.finditer(body):
            cand = _clean_entity_name(match.group(0))
            if not cand:
                continue
            counts[cand] = counts.get(cand, 0) + 1
            sources.setdefault(cand, "chunk")

    ordered = sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:max_entities]
    return tuple(
        ContextEntity(name=name, occurrences=count, source=sources[name])
        for name, count in ordered
    )


def _extract_sections(
    chunks: list[_ChunkRecord],
) -> tuple[ContextSection, ...]:
    """Distinct chunk sections, in order of first appearance."""
    seen: set[str] = set()
    out: list[ContextSection] = []
    for chunk in chunks:
        sec = (chunk.section or "").strip()
        if not sec or len(sec) > _SECTION_MAX_CHARS:
            continue
        if sec.lower() in seen:
            continue
        seen.add(sec.lower())
        out.append(ContextSection(title=sec, page=chunk.page_start))
    return tuple(out)


def _extract_workflow_stages(
    chunks: list[_ChunkRecord],
    entities: tuple[ContextEntity, ...],
) -> tuple[str, ...]:
    """Best-effort: any entity that starts with ``Stage`` /
    ``Phase`` / ``Step`` / ``Process`` is a workflow stage seed.

    Mostly useful for process / workflow-heavy documents
    (validation packets, runbooks, etc.). Falls back to empty
    when no such entity exists; the workflow generator then
    quietly emits zero cases.
    """
    keywords = ("stage", "phase", "step", "process")
    out: list[str] = []
    seen: set[str] = set()
    for ent in entities:
        first_word = ent.name.split()[0].lower() if ent.name else ""
        if first_word in keywords and ent.name.lower() not in seen:
            seen.add(ent.name.lower())
            out.append(ent.name)
    return tuple(out)


def _extract_relationships(
    *,
    enriched_artifacts: list[ArtifactRecord],
    graph_artifacts: list[ArtifactRecord],
) -> tuple[str, ...]:
    """Pull relationship phrases from the document_map artifact
    (when present)."""
    out: list[str] = []
    seen: set[str] = set()
    for art in enriched_artifacts:
        if art.kind != "enriched.document_map":
            continue
        for rel in _iter_document_map_relationships(art):
            if isinstance(rel, str) and rel.strip():
                key = rel.strip().lower()
                if key not in seen:
                    seen.add(key)
                    out.append(rel.strip())
    # Graph artifacts: ``metadata["relationship_types"]`` when present.
    for art in graph_artifacts:
        meta = art.metadata or {}
        rels = meta.get("relationship_types") or meta.get("top_relations")
        if isinstance(rels, (list, tuple)):
            for rel in rels:
                if isinstance(rel, str) and rel.strip():
                    key = rel.strip().lower()
                    if key not in seen:
                        seen.add(key)
                        out.append(rel.strip())
    return tuple(out)


def _extract_page_count(
    *,
    final_report: dict[str, Any] | None,
    chunks: list[_ChunkRecord],
) -> int | None:
    if final_report:
        cs = final_report.get("compile_summary") or {}
        pc = cs.get("page_count")
        if isinstance(pc, int) and pc > 0:
            return pc
    # Fallback: max page_end across chunks.
    best: int | None = None
    for chunk in chunks:
        for value in (chunk.page_start, chunk.page_end):
            if isinstance(value, int) and (best is None or value > best):
                best = value
    return best


# ---- Iteration helpers (defensive across artifact shapes) ----------


def _iter_graph_top_entities(art: ArtifactRecord) -> Iterable[str]:
    meta = art.metadata or {}
    candidates = (
        meta.get("top_entities")
        or meta.get("entities")
        or meta.get("named_entities")
    )
    if isinstance(candidates, (list, tuple)):
        for item in candidates:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                name = item.get("name") or item.get("label")
                if isinstance(name, str):
                    yield name


def _iter_document_map_entities(art: ArtifactRecord) -> Iterable[str]:
    """``enriched.document_map`` artifacts vary in shape — try the
    common slots. The metadata blob may carry the structured data
    OR a JSON-string payload; handle both."""
    meta = art.metadata or {}
    payload = _coerce_to_dict(meta.get("payload") or meta)
    if not payload:
        return
    for key in ("entities", "key_entities", "named_entities"):
        items = payload.get(key)
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("label")
                    if isinstance(name, str):
                        yield name


def _iter_document_map_relationships(art: ArtifactRecord) -> Iterable[str]:
    meta = art.metadata or {}
    payload = _coerce_to_dict(meta.get("payload") or meta)
    if not payload:
        return
    items = payload.get("relationships") or payload.get("relations")
    if isinstance(items, (list, tuple)):
        for item in items:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                label = (
                    item.get("label")
                    or item.get("name")
                    or item.get("type")
                )
                if isinstance(label, str):
                    yield label


def _coerce_to_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, str)):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ---- Text helpers ---------------------------------------------------


def _split_sentences(body: str, *, max_sentences: int) -> list[str]:
    """Cheap sentence splitter capped at ``max_sentences`` returned."""
    text = body.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts[:max_sentences] if p.strip()]


def _is_useful_sentence(sentence: str) -> bool:
    """Reject obvious non-sentence material (metadata blocks, code,
    overly-long paragraph dumps). The thresholds are tuned to
    accept short-but-useful sentences like "The proposal is due
    20 May 2026." while rejecting one-word fragments, pure
    numbers, and metadata pipe-tables."""
    s = sentence.strip()
    if len(s) < 18 or len(s) > 220:
        return False
    if "|" in s and s.count("|") >= 2:
        # "Doc ID: X | Version: Y | …" metadata blocks.
        return False
    if s.startswith(("- ", "* ", "• ", "  ")):
        return False
    # Must contain at least one alphabetic word + at least two
    # non-stopwords. Rules out "page 1", "[]", number-only lines
    # without rejecting short legitimate sentences.
    words = [
        w for w in re.findall(r"[A-Za-z]+", s)
        if w.lower() not in _STOPWORDS
    ]
    if len(words) < 2:
        return False
    return True


def _clean_entity_name(name: str) -> str | None:
    """Normalise the candidate and reject anything that would produce
    a low-value question. The previous filter was too permissive —
    it let through file-type words ("PDF"), determiners ("The"),
    UI labels ("Expected"), and identifier-style strings like
    "J1-CE-TEST-0426" that are answers, not topics.

    Rejection rules (post operator feedback):

      * empty / too long
      * all stopwords or all noise terms
      * single bare word matching ``NOISE_TERMS`` (PDF, The, …)
      * single word ≤ 3 chars (no semantic anchor)
      * identifier shape (digits + dash, mostly uppercase short
        token) — these belong as the ANSWER to a question, not
        the topic
      * pure numbers
    """
    s = (name or "").strip()
    if not s or len(s) > 80:
        return None
    words = s.split()
    if not words:
        return None
    if all(w.lower() in _STOPWORDS for w in words):
        return None
    # Bare single word: must not be on the noise list / stopwords.
    # Length is intentionally NOT a strong filter — real names
    # like "Bob" / acronyms like "API" should pass; the noise
    # list handles the bad 3-char tokens ("PDF", "The", "ID")
    # explicitly. Only single-char tokens get the length bar.
    if len(words) == 1:
        bare = words[0]
        lowered = bare.lower()
        if lowered in NOISE_TERMS:
            return None
        if lowered in _STOPWORDS:
            return None
        if len(bare) < 2:
            return None
    # Multi-word entities: reject when EVERY word is noise / stopword.
    if all(w.lower() in NOISE_TERMS | _STOPWORDS for w in words):
        return None
    if all(w.isdigit() for w in words):
        return None
    # Identifier-style strings (e.g. ``J1-CE-TEST-0426``,
    # ``DOT-2024``). These are great as ANSWERS but make
    # malformed questions when used as topics: "What is the X of
    # J1-CE-TEST-0426?" already names the identifier. Reject when
    # the candidate contains 2+ dashes or 1+ dashes AND a digit.
    if _looks_like_identifier(s):
        return None
    return s


def _looks_like_identifier(s: str) -> bool:
    """Identifier-style strings (``J1-CE-TEST-0426``, ``DOT-2024``,
    ``CB-2``) are answers, not question topics. Detect and reject
    so the topic extractor never asks "What is the X of <id>?"
    """
    if not s:
        return False
    # 2+ dashes is a strong identifier signal.
    if s.count("-") >= 2:
        return True
    # 1 dash AND at least one digit segment = also identifier.
    if "-" in s and re.search(r"\d", s):
        # Allow a single-letter-word + digit pair (e.g. "Stage 1"
        # split rendered as "Stage-1") via the workflow path.
        # Reject when the part before/after the dash is itself
        # short / nondescriptive (the typical id signature).
        parts = s.split("-")
        if all(len(p) <= 8 for p in parts):
            return True
    return False


def _clean_title(title: str) -> str:
    """Strip filename / extension noise from a chunk-derived title."""
    s = title.strip()
    # Drop trailing extensions ".pdf" / ".docx" / etc.
    s = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", s)
    # Collapse underscores → spaces (filenames are often
    # ``snake_case_document_name`` style).
    s = s.replace("_", " ")
    # Trim repeated whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalise_for_dedup(s: str) -> str:
    return _FACT_NORMALISE_RE.sub("", s).lower()
