"""Document Intent & Type Assessment.

Inferred *after* compile / Content Inventory but *before* the post-
compile execution-plan recommendations. Answers the questions the
post-compile planner needs to make good cost/quality trade-offs:

  * What kind of document is this?
  * What is it mainly about?
  * Who is it for?
  * Which analysis bias is appropriate?

Title-first heuristic: the cheapest reliable signal is usually the
title. We try every available title source — explicit metadata, the
parser's title block, the first heading, then the filename, then the
first-page digest, then early-page heading text. We classify the
title's quality (`clear` / `ambiguous` / `missing` / `generic`) and
drop to higher-cost signals only when needed.

Pure deterministic logic. No I/O, no LLM calls. Inputs are the
parsed-content manifest + a small `DocumentMetadata` view. Output is
a frozen dataclass the post-compile planner consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Iterable

from j1.processing.manifest import ParsedContentManifest


__all__ = [
    "DOCUMENT_TYPES",
    "DocumentMetadata",
    "DocumentType",
    "DocumentUnderstanding",
    "TITLE_QUALITY_AMBIGUOUS",
    "TITLE_QUALITY_CLEAR",
    "TITLE_QUALITY_GENERIC",
    "TITLE_QUALITY_MISSING",
    "TitleCandidate",
    "TitleEvidence",
    "assess_document_understanding",
]


# ---- Taxonomy ---------------------------------------------------------


class DocumentType(StrEnum):
    """User-facing document type taxonomy.

    Values are stable wire strings — used by the rule-based assessor,
    the LLM planner output schema, the FE Planning Report, and the
    audit log. New types are additive; never renumber / rename
    without a migration window.
    """

    POLICY = "policy"
    PROCEDURE = "procedure"
    STANDARD_OPERATING_PROCEDURE = "standard_operating_procedure"
    CONTRACT = "contract"
    LEGAL_DOCUMENT = "legal_document"
    TECHNICAL_DOCUMENT = "technical_document"
    SOFTWARE_ARCHITECTURE = "software_architecture"
    API_SPECIFICATION = "api_specification"
    BUSINESS_REQUIREMENT = "business_requirement"
    SYSTEM_REQUIREMENT_SPECIFICATION = "system_requirement_specification"
    PROPOSAL = "proposal"
    ESTIMATION = "estimation"
    PROJECT_PLAN = "project_plan"
    MEETING_MINUTES = "meeting_minutes"
    REPORT = "report"
    PRESENTATION = "presentation"
    FINANCIAL_DOCUMENT = "financial_document"
    INVOICE = "invoice"
    FORM = "form"
    USER_MANUAL = "user_manual"
    TRAINING_MATERIAL = "training_material"
    RESEARCH_PAPER = "research_paper"
    SOURCE_CODE_DOCUMENTATION = "source_code_documentation"
    UNKNOWN = "unknown"
    OTHER = "other"


DOCUMENT_TYPES: frozenset[str] = frozenset(t.value for t in DocumentType)


# ---- Title-quality vocabulary ----------------------------------------


TITLE_QUALITY_CLEAR = "clear"
TITLE_QUALITY_AMBIGUOUS = "ambiguous"
TITLE_QUALITY_MISSING = "missing"
TITLE_QUALITY_GENERIC = "generic"


# A short list of words that — alone — do NOT indicate a topic. The
# Final/Draft/v2 case is filename-style noise; Untitled/Scan is the
# null case; the rest are container-shaped (a doc isn't useful as
# "Document"). Operator-tunable via subclassing if a deployment has
# a different vocabulary; we deliberately don't expose this as env
# config because it's a heuristic, not a policy knob.
_GENERIC_TITLE_WORDS: frozenset[str] = frozenset({
    "document", "report", "proposal", "untitled",
    "scan", "export", "final", "version", "draft",
    "copy", "new", "v1", "v2", "v3",
})


# Hash-like: 8+ hex chars contiguous. Filename-like: contains an
# extension separator, or the entire string is `[name][_-][digits]+`.
_HASH_LIKE_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_FILENAME_LIKE_RE = re.compile(
    r"^(?:scan|doc|file|document|export|untitled)[_\- ]?\d+",
    re.IGNORECASE,
)
_DATE_CODE_RE = re.compile(
    r"^[\d_\-/. ]+$"  # only digits and date-shape separators
)


# ---- Type-detection keyword catalogue --------------------------------
#
# Tuple-based catalogue so the planner can attribute its decisions
# to specific evidence words ("title contained 'invoice number'").
# Keep the entries cheap to scan — substring matches on a lower-cased
# string. Higher-precision regexes go behind dedicated rule helpers
# below; this catalogue is for the breadth of common cues.

_TYPE_KEYWORDS: list[tuple[DocumentType, tuple[str, ...]]] = [
    (DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION, (
        "system requirement specification", "srs",
        "system requirements specification",
        "functional requirements", "non-functional requirements",
        "requirement specification",
    )),
    (DocumentType.BUSINESS_REQUIREMENT, (
        "business requirement", "brd", "business requirements document",
    )),
    (DocumentType.SOFTWARE_ARCHITECTURE, (
        "software architecture", "system architecture",
        "architecture overview", "architecture design",
        "high level design", "low level design",
        "component diagram", "deployment diagram",
    )),
    (DocumentType.API_SPECIFICATION, (
        "api specification", "api reference", "openapi", "swagger",
        "endpoint reference", "api documentation",
    )),
    (DocumentType.PROPOSAL, (
        "proposal", "tender", "request for proposal response",
        "statement of work",
    )),
    (DocumentType.ESTIMATION, (
        "estimation", "estimate", "effort estimate", "cost estimate",
        "level of effort", "loe", "wbs estimate",
    )),
    (DocumentType.PROJECT_PLAN, (
        "project plan", "project schedule", "project charter",
        "implementation plan", "rollout plan",
    )),
    (DocumentType.MEETING_MINUTES, (
        "meeting minutes", "minutes of meeting", "mom",
        "meeting notes", "weekly sync notes",
    )),
    (DocumentType.CONTRACT, (
        "contract", "master service agreement", "msa",
        "service agreement", "non-disclosure agreement", "nda",
        "purchase order",
    )),
    (DocumentType.LEGAL_DOCUMENT, (
        "terms and conditions", "privacy policy",
        "data processing agreement", "dpa",
        "license agreement", "legal notice",
    )),
    (DocumentType.POLICY, (
        "policy", "code of conduct", "acceptable use policy",
        "compliance policy",
    )),
    (DocumentType.STANDARD_OPERATING_PROCEDURE, (
        "standard operating procedure", "sop",
    )),
    (DocumentType.PROCEDURE, (
        "procedure", "operating procedure", "how to procedure",
    )),
    (DocumentType.INVOICE, (
        "invoice", "tax invoice", "bill to", "remittance",
        "invoice number", "invoice no.", "invoice no:",
    )),
    (DocumentType.FINANCIAL_DOCUMENT, (
        "balance sheet", "income statement", "cash flow",
        "financial report", "financial statement",
        "p&l", "profit and loss",
    )),
    (DocumentType.FORM, (
        "application form", "registration form",
        "intake form", "consent form",
    )),
    (DocumentType.USER_MANUAL, (
        "user manual", "user guide", "getting started",
        "installation guide", "operation manual",
    )),
    (DocumentType.TRAINING_MATERIAL, (
        "training material", "training guide", "course outline",
        "learning module",
    )),
    (DocumentType.RESEARCH_PAPER, (
        "abstract", "doi", "ieee", "acm", "preprint",
        "research paper", "conference paper",
    )),
    (DocumentType.SOURCE_CODE_DOCUMENTATION, (
        "javadoc", "doxygen", "rustdoc", "godoc",
        "source code documentation",
    )),
    (DocumentType.PRESENTATION, (
        "slide deck", "presentation", "keynote",
    )),
    (DocumentType.MEETING_MINUTES, (
        "action items", "decisions", "attendees",
    )),
    (DocumentType.REPORT, (
        "monthly report", "quarterly report", "annual report",
        "status report", "incident report",
    )),
    (DocumentType.TECHNICAL_DOCUMENT, (
        "technical specification", "technical design",
        "design document",
    )),
]


# Audience inference table — keyword → audience. Matched on title +
# heading outline. First match wins.
_AUDIENCE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("executive", ("executive summary", "board", "ceo", "cfo", "ciso")),
    ("legal_team", (
        "legal", "compliance", "regulatory", "data protection",
    )),
    ("technical_team", (
        "developer", "architecture", "api",
        "infrastructure", "devops",
    )),
    ("operations_team", ("operations", "runbook", "oncall", "support")),
    ("business_user", ("business", "requirements", "stakeholder")),
    ("customer", ("customer", "user manual", "getting started")),
]


# ---- Public dataclasses -----------------------------------------------


@dataclass(frozen=True)
class DocumentMetadata:
    """Lightweight view the assessor reads.

    Built by the workflow / activity layer from `IngestRequest`,
    `DocumentProfile`, and the run record. Pure data; no derivations
    happen here so the function stays trivially testable."""

    document_id: str
    filename: str | None = None
    mime_type: str | None = None
    extension: str | None = None
    metadata_title: str | None = None  # PDF /Title metadata, etc.
    language: str | None = None


@dataclass(frozen=True)
class TitleCandidate:
    """One title source the assessor considered."""

    source: str  # metadata|title_block|first_heading|filename|first_page
    text: str
    page: int | None = None
    score: float = 0.0


@dataclass(frozen=True)
class TitleEvidence:
    """One piece of evidence supporting the chosen type/title."""

    source: str  # title|heading|filename|first_page|early_page
    page: int | None
    text_preview: str
    reason: str


@dataclass(frozen=True)
class AnalysisBias:
    """Per-bias hints the rule-based assessor consumes when ranking
    enrichers. Plain bool flags so the consumer can OR them with its
    own signals; the `reason` carries operator-readable copy."""

    prefer_requirement_extraction: bool = False
    prefer_risk_extraction: bool = False
    prefer_table_enrichment: bool = False
    prefer_graph_extraction: bool = False
    prefer_visual_enrichment: bool = False
    prefer_quality_review: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DocumentUnderstanding:
    """Output of `assess_document_understanding`.

    Stable wire shape — the rule-based post-compile assessor and the
    LLM-assisted planner both consume it; the FE Planning Report
    renders it directly. New fields are additive; readers must
    tolerate missing keys."""

    title_source: str
    detected_title: str
    title_quality: str
    document_type: DocumentType
    document_type_confidence: float
    business_domain: str
    primary_topic: str
    document_purpose: str
    intended_audience: str
    document_importance: str
    expected_information_types: tuple[str, ...]
    recommended_analysis_bias: AnalysisBias
    title_candidates: tuple[TitleCandidate, ...]
    evidence: tuple[TitleEvidence, ...]
    warnings: tuple[str, ...] = ()


# ---- Public entry point ----------------------------------------------


def assess_document_understanding(
    *,
    metadata: DocumentMetadata,
    manifest: ParsedContentManifest | None,
    max_early_pages: int = 3,
) -> DocumentUnderstanding:
    """Run the title-first document-understanding rules.

    `manifest` is the parsed-content manifest emitted by compile;
    None when called for legacy runs that pre-date the artifact.
    `max_early_pages` caps how many early pages we inspect when the
    title is unclear — should match `J1_PLANNING_MAX_EARLY_PAGES`.

    Deterministic. Pure function. No I/O."""
    candidates = _collect_title_candidates(metadata, manifest)
    chosen, title_quality = _pick_title(candidates)
    detected_title = chosen.text if chosen else ""

    # Type detection: try title first; fall back to early-page heading
    # text + filename when the title is unclear.
    early_page_corpus = _collect_early_page_text(manifest, max_early_pages)
    detection_corpus = _build_detection_corpus(
        title=detected_title,
        early_pages=early_page_corpus,
        filename=metadata.filename,
        manifest=manifest,
    )
    document_type, type_confidence, type_evidence = _detect_document_type(
        title=detected_title,
        title_quality=title_quality,
        detection_corpus=detection_corpus,
        manifest=manifest,
        filename=metadata.filename,
    )

    # Topic / domain / purpose / audience: derived from the same
    # corpus; cheap heuristics only.
    primary_topic = _derive_primary_topic(
        title=detected_title,
        document_type=document_type,
        manifest=manifest,
    )
    business_domain = _derive_business_domain(
        detection_corpus, document_type,
    )
    document_purpose = _purpose_for_type(document_type, primary_topic)
    intended_audience = _derive_audience(
        title=detected_title,
        early_pages=early_page_corpus,
        document_type=document_type,
    )

    importance = _derive_importance(
        document_type=document_type,
        title_quality=title_quality,
        manifest=manifest,
    )
    expected_info = _expected_information_types(document_type)
    bias = _bias_for_type(document_type, manifest)
    warnings = _collect_warnings(
        title_quality=title_quality,
        document_type=document_type,
        type_confidence=type_confidence,
        manifest=manifest,
    )

    # Evidence list: the title source + the type-detection trigger.
    evidence: list[TitleEvidence] = []
    if chosen:
        evidence.append(TitleEvidence(
            source=_title_source_to_evidence_source(chosen.source),
            page=chosen.page,
            text_preview=_truncate(chosen.text, 160),
            reason=f"title quality={title_quality}",
        ))
    evidence.extend(type_evidence)

    return DocumentUnderstanding(
        title_source=chosen.source if chosen else "unknown",
        detected_title=detected_title,
        title_quality=title_quality,
        document_type=document_type,
        document_type_confidence=type_confidence,
        business_domain=business_domain,
        primary_topic=primary_topic,
        document_purpose=document_purpose,
        intended_audience=intended_audience,
        document_importance=importance,
        expected_information_types=tuple(expected_info),
        recommended_analysis_bias=bias,
        title_candidates=tuple(candidates),
        evidence=tuple(evidence),
        warnings=tuple(warnings),
    )


# ---- Title collection -------------------------------------------------


def _collect_title_candidates(
    metadata: DocumentMetadata,
    manifest: ParsedContentManifest | None,
) -> list[TitleCandidate]:
    """Emit every title candidate, in declining-quality order.

    The caller (`_pick_title`) walks the list and stops at the first
    candidate that scores `clear`. Order matters: explicit metadata
    title is the strongest signal, filename is the weakest."""
    candidates: list[TitleCandidate] = []

    # 1. Explicit metadata title (e.g. PDF /Title key).
    if metadata.metadata_title and metadata.metadata_title.strip():
        candidates.append(TitleCandidate(
            source="metadata",
            text=metadata.metadata_title.strip(),
            score=0.95,
        ))

    # 2. Title-typed item from the parser, if surfaced.
    if manifest is not None:
        for item in manifest.items:
            if item.type and item.type.lower() == "title" and item.text_preview:
                candidates.append(TitleCandidate(
                    source="title_block",
                    text=item.text_preview.strip(),
                    page=item.page_idx,
                    score=0.9,
                ))
                break

        # 3. First heading on page 1 — heading items, ordered by page.
        for item in manifest.items:
            if (
                item.type and item.type.lower() in {"heading", "h1", "title"}
                and item.text_preview
            ):
                candidates.append(TitleCandidate(
                    source="first_heading",
                    text=item.text_preview.strip(),
                    page=item.page_idx,
                    score=0.7,
                ))
                break

    # 4. Filename (without extension). Always present unless the run
    # didn't capture it, but lowest score by default.
    if metadata.filename:
        stem = PurePosixPath(metadata.filename).stem
        if stem:
            candidates.append(TitleCandidate(
                source="filename",
                text=_humanize_filename_stem(stem),
                score=0.4,
            ))

    # 5. First-page heading-ish item (whatever heading appears first).
    # Distinct from `first_heading` above which only matched exact
    # heading types — this catches subtitled docs that bury the H1.
    if manifest is not None:
        for item in manifest.items:
            if item.page_idx == 1 and item.text_preview:
                if item.type and item.type.lower() not in {
                    "title", "heading", "h1",
                }:
                    continue
                candidates.append(TitleCandidate(
                    source="first_page",
                    text=item.text_preview.strip(),
                    page=1,
                    score=0.5,
                ))
                break

    return candidates


def _pick_title(
    candidates: list[TitleCandidate],
) -> tuple[TitleCandidate | None, str]:
    """Pick the best title candidate + grade its quality.

    Returns `(candidate, quality)` where quality is one of
    `TITLE_QUALITY_CLEAR / AMBIGUOUS / MISSING / GENERIC`."""
    if not candidates:
        return None, TITLE_QUALITY_MISSING

    # Walk in catalog order. We accept the first non-junk candidate.
    for cand in candidates:
        quality = _grade_title(cand.text)
        if quality == TITLE_QUALITY_CLEAR:
            return cand, TITLE_QUALITY_CLEAR
        if quality == TITLE_QUALITY_AMBIGUOUS:
            # Keep walking — maybe a later candidate is better. If
            # nothing better, this one wins.
            continue
        if quality == TITLE_QUALITY_GENERIC:
            continue
    # Fall back to the first candidate; rate its quality.
    fallback = candidates[0]
    return fallback, _grade_title(fallback.text)


def _grade_title(text: str) -> str:
    """Quality label for a candidate string."""
    if not text or not text.strip():
        return TITLE_QUALITY_MISSING
    stripped = text.strip()
    lower = stripped.lower()

    # Hash-like or pure date/code → ambiguous.
    if _HASH_LIKE_RE.search(stripped) and len(stripped) <= 24:
        return TITLE_QUALITY_AMBIGUOUS
    if _DATE_CODE_RE.match(stripped):
        return TITLE_QUALITY_GENERIC
    if _FILENAME_LIKE_RE.match(stripped):
        return TITLE_QUALITY_GENERIC

    words = [w for w in re.split(r"[\s_\-/]+", stripped) if w]
    meaningful = [w for w in words if w.lower() not in _GENERIC_TITLE_WORDS]
    if not meaningful:
        return TITLE_QUALITY_GENERIC
    if all(w.lower() in _GENERIC_TITLE_WORDS for w in words):
        return TITLE_QUALITY_GENERIC
    if len(meaningful) < 4 and lower in _GENERIC_TITLE_WORDS:
        return TITLE_QUALITY_GENERIC
    if len(meaningful) < 2:
        return TITLE_QUALITY_AMBIGUOUS
    if len(meaningful) < 4 and not _has_topic_word(meaningful):
        return TITLE_QUALITY_AMBIGUOUS
    return TITLE_QUALITY_CLEAR


def _has_topic_word(words: list[str]) -> bool:
    """A title is more likely 'clear' when it contains a topic noun
    rather than only generic structure words. We approximate this by
    checking for words longer than 4 chars that aren't in the generic
    vocabulary — a rough proxy for "carries information"."""
    return any(len(w) > 4 and w.lower() not in _GENERIC_TITLE_WORDS for w in words)


# ---- Type detection ---------------------------------------------------


def _detect_document_type(
    *,
    title: str,
    title_quality: str,
    detection_corpus: str,
    manifest: ParsedContentManifest | None,
    filename: str | None,
) -> tuple[DocumentType, float, list[TitleEvidence]]:
    """Match the title + early-page corpus + filename against the
    keyword catalogue. Returns `(type, confidence, evidence)`.

    Confidence scoring: `clear` title hit → 0.85; `ambiguous` title
    hit → 0.65; corpus-only match → 0.55; filename-only → 0.4;
    no match → 0.3 (UNKNOWN)."""
    haystack = detection_corpus.lower()
    title_lower = title.lower()
    filename_lower = (filename or "").lower()

    matches: list[tuple[DocumentType, float, str, str]] = []
    for doc_type, keywords in _TYPE_KEYWORDS:
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in title_lower:
                base = 0.85 if title_quality == TITLE_QUALITY_CLEAR else 0.65
                matches.append((doc_type, base, "title", kw))
                break
            if kw_lower in haystack:
                matches.append((doc_type, 0.55, "early_page", kw))
                break
            if kw_lower in filename_lower:
                matches.append((doc_type, 0.4, "filename", kw))
                break

    if not matches:
        # Structural fallbacks: a manifest with high table count
        # relative to text and the filename hints "invoice/receipt"
        # → invoice; presentation extension → presentation.
        if manifest is not None:
            stats = manifest.stats
            if stats.tables and stats.tables >= 3 and stats.text_blocks <= 30:
                return (
                    DocumentType.FINANCIAL_DOCUMENT, 0.45,
                    [TitleEvidence(
                        source="early_page", page=None,
                        text_preview="",
                        reason=(
                            f"structural fallback: tables={stats.tables}, "
                            f"text_blocks={stats.text_blocks}"
                        ),
                    )],
                )
        if filename:
            ext = PurePosixPath(filename).suffix.lower()
            if ext in {".pptx", ".ppt", ".key"}:
                return (
                    DocumentType.PRESENTATION, 0.5,
                    [TitleEvidence(
                        source="filename", page=None,
                        text_preview=filename,
                        reason=f"presentation extension {ext}",
                    )],
                )
        return DocumentType.UNKNOWN, 0.3, []

    # Pick the highest-confidence match. Tie → first wins (catalogue
    # order = priority).
    matches.sort(key=lambda m: m[1], reverse=True)
    best_type, best_conf, best_source, best_kw = matches[0]
    evidence = [TitleEvidence(
        source=best_source,
        page=None,
        text_preview=_truncate(title or filename or "", 120),
        reason=f"matched keyword {best_kw!r}",
    )]
    # Attach a second piece of evidence from a competing type if any
    # (helps reviewers understand why we picked X over Y).
    if len(matches) > 1 and matches[1][1] >= best_conf - 0.1:
        evidence.append(TitleEvidence(
            source=matches[1][2],
            page=None,
            text_preview="",
            reason=(
                f"also considered {matches[1][0].value} via "
                f"{matches[1][3]!r}"
            ),
        ))
    return best_type, best_conf, evidence


# ---- Topic / domain / purpose / audience -----------------------------


def _derive_primary_topic(
    *, title: str, document_type: DocumentType,
    manifest: ParsedContentManifest | None,
) -> str:
    """Best-effort one-line topic. Falls back to the document type
    label when no better signal is available."""
    if title and len(title.split()) >= 3:
        return _truncate(title, 120)
    # Try the first heading from the manifest.
    if manifest is not None:
        for item in manifest.items:
            if item.type and item.type.lower() in {"heading", "h1", "title"}:
                if item.text_preview:
                    return _truncate(item.text_preview, 120)
                break
    return document_type.value.replace("_", " ")


def _derive_business_domain(corpus: str, document_type: DocumentType) -> str:
    """Coarse domain bucket. Catalogue-based; missing domains fall
    back to an empty string."""
    haystack = corpus.lower()
    domains = [
        ("finance", ("revenue", "ebitda", "fiscal", "tax", "invoice")),
        ("legal", ("clause", "indemnity", "liability", "jurisdiction")),
        ("software", ("api", "service", "deployment", "kubernetes", "cloud")),
        ("hr", ("employee", "payroll", "onboarding", "benefits")),
        ("sales", ("pipeline", "quota", "lead", "crm")),
        ("operations", ("incident", "oncall", "runbook", "sla")),
    ]
    for domain, keywords in domains:
        if any(k in haystack for k in keywords):
            return domain
    # Type-derived defaults.
    type_defaults = {
        DocumentType.INVOICE: "finance",
        DocumentType.FINANCIAL_DOCUMENT: "finance",
        DocumentType.CONTRACT: "legal",
        DocumentType.LEGAL_DOCUMENT: "legal",
        DocumentType.SOFTWARE_ARCHITECTURE: "software",
        DocumentType.API_SPECIFICATION: "software",
    }
    return type_defaults.get(document_type, "")


def _purpose_for_type(document_type: DocumentType, topic: str) -> str:
    """One-line purpose string. Stable copy by type."""
    purposes = {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION:
            "Specify functional and non-functional system requirements.",
        DocumentType.BUSINESS_REQUIREMENT:
            "Capture business requirements and stakeholder needs.",
        DocumentType.SOFTWARE_ARCHITECTURE:
            "Describe system components, interactions, and design decisions.",
        DocumentType.API_SPECIFICATION:
            "Document API endpoints, payloads, and contracts.",
        DocumentType.PROPOSAL:
            "Present a project proposal with scope, approach, and pricing.",
        DocumentType.ESTIMATION:
            "Estimate effort, duration, and cost.",
        DocumentType.PROJECT_PLAN:
            "Plan project execution: scope, schedule, owners, risks.",
        DocumentType.MEETING_MINUTES:
            "Record decisions, action items, and attendees.",
        DocumentType.CONTRACT:
            "Establish a binding agreement between parties.",
        DocumentType.LEGAL_DOCUMENT:
            "Communicate legal terms or compliance obligations.",
        DocumentType.POLICY:
            "Define organisational rules and obligations.",
        DocumentType.PROCEDURE:
            "Describe a repeatable operational procedure.",
        DocumentType.STANDARD_OPERATING_PROCEDURE:
            "Standardise an operational task end-to-end.",
        DocumentType.INVOICE:
            "Bill for goods or services rendered.",
        DocumentType.FINANCIAL_DOCUMENT:
            "Communicate financial position or activity.",
        DocumentType.FORM:
            "Collect structured input from a respondent.",
        DocumentType.USER_MANUAL:
            "Help a user operate or install a product.",
        DocumentType.TRAINING_MATERIAL:
            "Teach a topic to a learner.",
        DocumentType.RESEARCH_PAPER:
            "Present research findings.",
        DocumentType.SOURCE_CODE_DOCUMENTATION:
            "Document a codebase or library.",
        DocumentType.PRESENTATION:
            "Present material visually to an audience.",
        DocumentType.REPORT:
            "Report on activities or status.",
        DocumentType.TECHNICAL_DOCUMENT:
            "Document a technical subject.",
    }
    return purposes.get(document_type, f"Reference document about {topic}." if topic else "")


def _derive_audience(
    *, title: str, early_pages: str, document_type: DocumentType,
) -> str:
    haystack = (title + " " + early_pages).lower()
    for audience, keywords in _AUDIENCE_KEYWORDS:
        if any(k in haystack for k in keywords):
            return audience
    # Type-derived defaults.
    type_defaults = {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION: "technical_team",
        DocumentType.BUSINESS_REQUIREMENT: "business_user",
        DocumentType.SOFTWARE_ARCHITECTURE: "technical_team",
        DocumentType.API_SPECIFICATION: "technical_team",
        DocumentType.CONTRACT: "legal_team",
        DocumentType.LEGAL_DOCUMENT: "legal_team",
        DocumentType.POLICY: "operations_team",
        DocumentType.PROCEDURE: "operations_team",
        DocumentType.STANDARD_OPERATING_PROCEDURE: "operations_team",
        DocumentType.INVOICE: "business_user",
        DocumentType.FINANCIAL_DOCUMENT: "executive",
        DocumentType.USER_MANUAL: "customer",
        DocumentType.TRAINING_MATERIAL: "business_user",
    }
    return type_defaults.get(document_type, "unknown")


def _derive_importance(
    *,
    document_type: DocumentType,
    title_quality: str,
    manifest: ParsedContentManifest | None,
) -> str:
    """Coarse importance bucket. High-value types default to high;
    `unknown` with weak title evidence defaults to low."""
    high_value = {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION,
        DocumentType.BUSINESS_REQUIREMENT,
        DocumentType.SOFTWARE_ARCHITECTURE,
        DocumentType.PROPOSAL,
        DocumentType.ESTIMATION,
        DocumentType.CONTRACT,
        DocumentType.LEGAL_DOCUMENT,
        DocumentType.POLICY,
        DocumentType.PROJECT_PLAN,
    }
    low_value = {
        DocumentType.INVOICE, DocumentType.FORM,
    }
    if document_type in high_value:
        return "high"
    if document_type in low_value:
        return "low"
    if document_type == DocumentType.UNKNOWN:
        return "low" if title_quality in (
            TITLE_QUALITY_MISSING, TITLE_QUALITY_GENERIC,
        ) else "medium"
    if manifest is not None and (manifest.stats.page_count or 0) > 30:
        return "medium"
    return "medium"


def _expected_information_types(document_type: DocumentType) -> list[str]:
    """The taxonomy entry on the wire schema's
    `expected_information_types`. Stable per-type list."""
    by_type: dict[DocumentType, list[str]] = {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION: [
            "requirements", "responsibilities", "tables",
            "entities_and_relationships",
        ],
        DocumentType.BUSINESS_REQUIREMENT: [
            "requirements", "responsibilities", "decisions",
        ],
        DocumentType.SOFTWARE_ARCHITECTURE: [
            "technical_components", "entities_and_relationships",
            "decisions",
        ],
        DocumentType.API_SPECIFICATION: [
            "technical_components", "tables",
        ],
        DocumentType.PROPOSAL: [
            "requirements", "risks", "tables", "financial_values",
        ],
        DocumentType.ESTIMATION: [
            "tables", "financial_values", "risks",
        ],
        DocumentType.PROJECT_PLAN: [
            "decisions", "responsibilities", "risks", "process_steps",
        ],
        DocumentType.MEETING_MINUTES: [
            "decisions", "responsibilities",
        ],
        DocumentType.CONTRACT: [
            "legal_terms", "responsibilities", "financial_values",
        ],
        DocumentType.LEGAL_DOCUMENT: [
            "legal_terms",
        ],
        DocumentType.POLICY: [
            "legal_terms", "responsibilities", "process_steps",
        ],
        DocumentType.PROCEDURE: ["process_steps"],
        DocumentType.STANDARD_OPERATING_PROCEDURE: ["process_steps"],
        DocumentType.INVOICE: ["financial_values", "tables"],
        DocumentType.FINANCIAL_DOCUMENT: ["financial_values", "tables"],
        DocumentType.FORM: ["tables"],
        DocumentType.USER_MANUAL: ["process_steps", "technical_components"],
        DocumentType.TRAINING_MATERIAL: ["process_steps"],
        DocumentType.RESEARCH_PAPER: ["technical_components", "tables"],
        DocumentType.SOURCE_CODE_DOCUMENTATION: [
            "technical_components", "process_steps",
        ],
        DocumentType.PRESENTATION: ["decisions"],
        DocumentType.REPORT: ["tables", "decisions"],
        DocumentType.TECHNICAL_DOCUMENT: [
            "technical_components", "tables",
        ],
    }
    return by_type.get(document_type, [])


def _bias_for_type(
    document_type: DocumentType,
    manifest: ParsedContentManifest | None,
) -> AnalysisBias:
    """Per-type analysis bias. The rule-based assessor reads these
    flags to gate its enrichment recommendations.

    The reason field is operator-readable and surfaces in the FE
    Planning Report's "why this plan" panel."""
    has_tables = bool(manifest and manifest.stats.tables)
    has_images = bool(manifest and manifest.stats.images)

    if document_type in {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION,
        DocumentType.BUSINESS_REQUIREMENT,
    }:
        return AnalysisBias(
            prefer_requirement_extraction=True,
            prefer_risk_extraction=True,
            prefer_table_enrichment=has_tables,
            prefer_graph_extraction=True,
            prefer_quality_review=True,
            reason="Requirement document — extract requirements, risks, owners; build graph for relationships.",
        )
    if document_type in {
        DocumentType.SOFTWARE_ARCHITECTURE,
        DocumentType.API_SPECIFICATION,
        DocumentType.TECHNICAL_DOCUMENT,
    }:
        return AnalysisBias(
            prefer_table_enrichment=has_tables,
            prefer_graph_extraction=True,
            prefer_visual_enrichment=has_images,
            reason="Technical document — preserve structural detail; build component graph; describe diagrams.",
        )
    if document_type == DocumentType.PROPOSAL:
        return AnalysisBias(
            prefer_requirement_extraction=True,
            prefer_risk_extraction=True,
            prefer_table_enrichment=has_tables,
            prefer_quality_review=True,
            reason="Proposal — extract assumptions, costs, and risks from tables and narrative.",
        )
    if document_type == DocumentType.ESTIMATION:
        return AnalysisBias(
            prefer_table_enrichment=True,
            prefer_risk_extraction=True,
            prefer_quality_review=True,
            reason="Estimation — table-heavy; extract effort/cost values and risks.",
        )
    if document_type == DocumentType.PROJECT_PLAN:
        return AnalysisBias(
            prefer_risk_extraction=True,
            prefer_graph_extraction=True,
            reason="Project plan — extract decisions, owners, and risk register.",
        )
    if document_type in {
        DocumentType.CONTRACT,
        DocumentType.LEGAL_DOCUMENT,
        DocumentType.POLICY,
    }:
        return AnalysisBias(
            prefer_risk_extraction=True,
            prefer_quality_review=True,
            reason="Legal/policy — extract obligations, risks; careful citation metadata.",
        )
    if document_type == DocumentType.MEETING_MINUTES:
        return AnalysisBias(
            reason="Meeting minutes — extract decisions and action items if supported.",
        )
    if document_type == DocumentType.PRESENTATION:
        return AnalysisBias(
            prefer_visual_enrichment=has_images,
            reason="Presentation — visual enrichment only for meaningful diagrams.",
        )
    if document_type in {DocumentType.INVOICE, DocumentType.FINANCIAL_DOCUMENT}:
        return AnalysisBias(
            prefer_table_enrichment=True,
            reason="Financial document — table/key-value extraction; skip narrative enrichers.",
        )
    if document_type == DocumentType.UNKNOWN:
        return AnalysisBias(
            reason="Unknown document type — keep enrichment conservative until evidence supports more.",
        )
    return AnalysisBias(
        reason="Default analysis bias; no type-specific preferences.",
    )


# ---- Helpers ----------------------------------------------------------


def _collect_early_page_text(
    manifest: ParsedContentManifest | None,
    max_early_pages: int,
) -> str:
    """Concatenate text previews from the first `max_early_pages`
    pages of the manifest. Used as a corpus for keyword detection.
    No raw document content; only the parser's previews."""
    if manifest is None or not manifest.items:
        return ""
    parts: list[str] = []
    for item in manifest.items:
        page = item.page_idx
        if page is not None and page > max_early_pages:
            continue
        if item.text_preview:
            parts.append(item.text_preview)
    return " ".join(parts)


def _build_detection_corpus(
    *,
    title: str,
    early_pages: str,
    filename: str | None,
    manifest: ParsedContentManifest | None,
) -> str:
    """Concatenate every signal the keyword catalogue should scan."""
    parts: list[str] = []
    if title:
        parts.append(title)
    if early_pages:
        parts.append(early_pages)
    if filename:
        parts.append(filename)
    if manifest is not None:
        # Heading-only quick pass — headings often carry the strongest
        # type cues.
        for item in manifest.items:
            if item.type and item.type.lower() in {
                "heading", "h1", "h2", "h3", "title",
            } and item.text_preview:
                parts.append(item.text_preview)
    return "\n".join(parts)


def _humanize_filename_stem(stem: str) -> str:
    """`scan_2025_05_01-final` → `scan 2025 05 01 final`. Used so the
    title grader doesn't have to think about underscores/dashes."""
    return re.sub(r"[_\-]+", " ", stem).strip()


def _title_source_to_evidence_source(source: str) -> str:
    """Map TitleCandidate.source to the wire schema's evidence
    source vocabulary."""
    return {
        "metadata": "title",
        "title_block": "title",
        "first_heading": "heading",
        "filename": "filename",
        "first_page": "first_page",
    }.get(source, "title")


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _collect_warnings(
    *,
    title_quality: str,
    document_type: DocumentType,
    type_confidence: float,
    manifest: ParsedContentManifest | None,
) -> list[str]:
    out: list[str] = []
    if title_quality in (TITLE_QUALITY_MISSING, TITLE_QUALITY_GENERIC):
        out.append("Document title is missing or generic; type detection used fallback signals.")
    if document_type == DocumentType.UNKNOWN:
        out.append("Document type could not be confidently detected.")
    elif type_confidence < 0.5:
        out.append(
            f"Document type {document_type.value} detected with low confidence "
            f"({type_confidence:.2f}); plan stays conservative."
        )
    if manifest is not None:
        if (manifest.stats.parse_quality_score or 1.0) < 0.5:
            out.append("Parse quality score is low; consider manual review.")
        if (manifest.stats.text_extractable_ratio or 1.0) < 0.3:
            out.append(
                "Text-extractable ratio is low — likely scanned content; OCR / vision may be required."
            )
    return out
