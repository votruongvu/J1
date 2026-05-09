"""Rule-based post-compile assessment.

Consumes the parsed-content manifest, the merged DocumentProfile, the
Lightweight Content Digest, and the Document Understanding output to
produce a structured Processing Plan: per-step `enabled / scope /
reason` recommendations the workflow can use to gate downstream
enrichment, graph extraction, and indexing.

Pure function, deterministic, no LLM. The LLM-assisted planner runs
*on top of* this module's output: it sees the rule-based plan and may
override or accept individual recommendations.

Output uses simple frozen dataclasses (not Pydantic) so the workflow
can pass them through Temporal's data converter without extra
serialisation gymnastics."""

from __future__ import annotations

from dataclasses import dataclass, field

from j1.processing.content_digest import ContentDigest
from j1.processing.document_understanding import (
    DocumentType,
    DocumentUnderstanding,
)
from j1.processing.manifest import ParsedContentManifest
from j1.processing.profiling import DocumentProfile


__all__ = [
    "ChunkingRecommendation",
    "ContentReport",
    "ExecutionPlan",
    "PostCompileAssessment",
    "PostCompileSignals",
    "QualityReport",
    "QualityIssue",
    "ReviewCandidate",
    "StepRecommendation",
    "build_post_compile_assessment",
]


# ---- Vocabulary -------------------------------------------------------

PROFILE_FAST = "fast"
PROFILE_BALANCED = "balanced"
PROFILE_PREMIUM = "premium"
PROFILE_DIAGNOSTIC = "diagnostic"
PROFILE_CUSTOM = "custom"

ALLOWED_PROFILES: frozenset[str] = frozenset({
    PROFILE_FAST, PROFILE_BALANCED, PROFILE_PREMIUM,
    PROFILE_DIAGNOSTIC, PROFILE_CUSTOM,
})

# Step-name vocabulary mirrors the wire schema (snake_case).
STEP_CHUNKING = "chunking"
STEP_TABLE_ENRICHMENT = "table_enrichment"
STEP_VISION_ENRICHMENT = "vision_enrichment"
STEP_IMAGE_CAPTIONING = "image_captioning"
STEP_REQUIREMENT_EXTRACTION = "requirement_extraction"
STEP_RISK_EXTRACTION = "risk_extraction"
STEP_QUALITY_ASSESSMENT = "quality_assessment"
STEP_GRAPH_EXTRACTION = "graph_extraction"
STEP_EMBEDDING = "embedding"
STEP_INDEXING = "indexing"

ALL_STEP_NAMES: tuple[str, ...] = (
    STEP_CHUNKING,
    STEP_TABLE_ENRICHMENT,
    STEP_VISION_ENRICHMENT,
    STEP_IMAGE_CAPTIONING,
    STEP_REQUIREMENT_EXTRACTION,
    STEP_RISK_EXTRACTION,
    STEP_QUALITY_ASSESSMENT,
    STEP_GRAPH_EXTRACTION,
    STEP_EMBEDDING,
    STEP_INDEXING,
)


CHUNK_STRATEGY_SECTION_AWARE = "section_aware"
CHUNK_STRATEGY_PAGE_AWARE = "page_aware"
CHUNK_STRATEGY_SEMANTIC = "semantic"
CHUNK_STRATEGY_FIXED_SIZE = "fixed_size"
CHUNK_STRATEGY_HYBRID = "hybrid"

ALLOWED_CHUNK_STRATEGIES: frozenset[str] = frozenset({
    CHUNK_STRATEGY_SECTION_AWARE,
    CHUNK_STRATEGY_PAGE_AWARE,
    CHUNK_STRATEGY_SEMANTIC,
    CHUNK_STRATEGY_FIXED_SIZE,
    CHUNK_STRATEGY_HYBRID,
})


SCOPE_NONE = "none"
SCOPE_DOCUMENT = "document"
SCOPE_TABLES_ONLY = "tables_only"
SCOPE_SELECTED_PAGES = "selected_pages"
SCOPE_SELECTED_SECTIONS = "selected_sections"
SCOPE_SELECTED_IMAGES = "selected_images"
SCOPE_ALL_IMAGES = "all_images"
SCOPE_ALL_IMAGE_PAGES = "all_image_pages"
SCOPE_LOW_CONFIDENCE_PAGES = "low_confidence_pages"

ALLOWED_SCOPES: frozenset[str] = frozenset({
    SCOPE_NONE, SCOPE_DOCUMENT, SCOPE_TABLES_ONLY,
    SCOPE_SELECTED_PAGES, SCOPE_SELECTED_SECTIONS,
    SCOPE_SELECTED_IMAGES, SCOPE_ALL_IMAGES, SCOPE_ALL_IMAGE_PAGES,
    SCOPE_LOW_CONFIDENCE_PAGES,
})


# ---- Output dataclasses -----------------------------------------------


@dataclass(frozen=True)
class StepRecommendation:
    """One per-step recommendation. `scope=none` for disabled steps;
    otherwise scope describes the breadth of the step's work."""

    step: str
    enabled: bool
    scope: str
    reason: str
    pages: tuple[int, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    candidate_entity_types: tuple[str, ...] = ()
    model_profile: str | None = None


@dataclass(frozen=True)
class ChunkingRecommendation:
    """Specialised step record for chunking — strategy + per-strategy
    knobs the chunker reads."""

    enabled: bool
    strategy: str
    reason: str
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PostCompileSignals:
    """Aggregate signals that drove the recommendations. The audit log
    + Planning Report render these so reviewers can trace decisions."""

    has_clear_headings: bool
    has_meaningful_tables: bool
    has_meaningful_images: bool
    has_ocr_or_scanned_pages: bool
    has_low_confidence_blocks: bool
    likely_graph_candidate: bool
    likely_requirement_document: bool
    likely_financial_document: bool
    likely_technical_document: bool


@dataclass(frozen=True)
class ContentReport:
    """Content-shape summary for the FE Planning Report tab.

    Mirrors the LLM-output schema's `content_report` so the FE has a
    single source of truth regardless of whether the rule-based or
    the LLM-assisted plan won."""

    language: str | None
    page_count: int | None
    structure_quality: str  # poor|fair|good|excellent
    layout_complexity: str  # low|medium|high
    content_density: str  # low|medium|high
    has_clear_sections: bool
    has_tables: bool
    has_images: bool
    has_formulas: bool
    has_ocr_pages: bool
    important_observations: tuple[str, ...]


@dataclass(frozen=True)
class QualityIssue:
    issue: str
    severity: str  # low|medium|high
    affected_pages: tuple[int, ...]
    recommendation: str


@dataclass(frozen=True)
class ReviewCandidate:
    page: int
    reason: str
    block_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityReport:
    parse_confidence: str  # low|medium|high
    risk_level: str  # low|medium|high
    detected_issues: tuple[QualityIssue, ...]
    manual_review_required: bool
    manual_review_candidates: tuple[ReviewCandidate, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    """The executable per-step plan the workflow gates on."""

    estimated_time: str  # low|medium|high
    estimated_cost: str  # low|medium|high
    chunking: ChunkingRecommendation
    steps: tuple[StepRecommendation, ...]


@dataclass(frozen=True)
class PostCompileAssessment:
    """Top-level rule-based assessment output."""

    recommended_profile: str
    confidence: float
    document_understanding: DocumentUnderstanding
    signals: PostCompileSignals
    content_report: ContentReport
    quality_report: QualityReport
    execution_plan: ExecutionPlan
    decision_summary_main_reasoning: tuple[str, ...]
    overall_assessment: str
    warnings: tuple[str, ...] = ()


# ---- Public entry point ----------------------------------------------


def build_post_compile_assessment(
    *,
    understanding: DocumentUnderstanding,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
    digest: ContentDigest,
) -> PostCompileAssessment:
    """Run the rule-based post-compile assessment.

    Pure deterministic function. Composes a Processing Plan from:
      * The Document Understanding output (type / bias / importance).
      * The parsed-content manifest (counts, parse quality scores).
      * The merged DocumentProfile (extension, scanned-page hints,
        text-extractability ratio).
      * The Lightweight Content Digest (heading outline, page mix).

    Caller decides whether to layer LLM-assisted overrides on top."""
    signals = _derive_signals(
        understanding=understanding,
        manifest=manifest,
        profile=profile,
        digest=digest,
    )
    chunking = _recommend_chunking(
        understanding=understanding,
        signals=signals,
        manifest=manifest,
        digest=digest,
    )
    steps = _recommend_steps(
        understanding=understanding,
        signals=signals,
        manifest=manifest,
        profile=profile,
    )
    profile_label = _pick_profile(
        understanding=understanding,
        signals=signals,
        manifest=manifest,
        profile=profile,
    )
    confidence = _confidence_for_assessment(
        understanding=understanding,
        signals=signals,
        manifest=manifest,
    )
    estimated_time, estimated_cost = _estimate_resources(steps, signals)
    content_report = _build_content_report(
        manifest=manifest,
        profile=profile,
        signals=signals,
        digest=digest,
    )
    quality_report = _build_quality_report(
        manifest=manifest,
        profile=profile,
        signals=signals,
    )
    main_reasoning = _build_main_reasoning(
        understanding=understanding, signals=signals,
        profile_label=profile_label,
    )
    overall = _overall_assessment(
        profile_label=profile_label,
        understanding=understanding,
        signals=signals,
    )

    plan = ExecutionPlan(
        estimated_time=estimated_time,
        estimated_cost=estimated_cost,
        chunking=chunking,
        steps=tuple(steps),
    )

    warnings: list[str] = []
    warnings.extend(understanding.warnings)
    if not signals.has_clear_headings and chunking.strategy == CHUNK_STRATEGY_SECTION_AWARE:
        warnings.append("section_aware chunking selected but heading signal is weak.")

    return PostCompileAssessment(
        recommended_profile=profile_label,
        confidence=confidence,
        document_understanding=understanding,
        signals=signals,
        content_report=content_report,
        quality_report=quality_report,
        execution_plan=plan,
        decision_summary_main_reasoning=tuple(main_reasoning),
        overall_assessment=overall,
        warnings=tuple(warnings),
    )


# ---- Signal derivation ------------------------------------------------


def _derive_signals(
    *,
    understanding: DocumentUnderstanding,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
    digest: ContentDigest,
) -> PostCompileSignals:
    """Boolean signal panel — every downstream rule reads from here."""
    has_headings = bool(digest.heading_outline)
    has_tables = bool(manifest and manifest.stats.tables)
    has_images = bool(manifest and manifest.stats.images)
    scanned_signal = (
        bool(profile and profile.has_scanned_pages)
        or bool(manifest and (manifest.stats.scanned_pages or 0) > 0)
        or bool(profile and profile.text_extractable_ratio is not None
                and profile.text_extractable_ratio < 0.3)
    )
    parse_quality = (
        manifest.stats.parse_quality_score
        if manifest and manifest.stats.parse_quality_score is not None
        else 1.0
    )
    has_low_conf = parse_quality < 0.6 or scanned_signal

    likely_graph = understanding.recommended_analysis_bias.prefer_graph_extraction
    likely_req = understanding.recommended_analysis_bias.prefer_requirement_extraction
    likely_finance = understanding.document_type in {
        DocumentType.INVOICE,
        DocumentType.FINANCIAL_DOCUMENT,
        DocumentType.ESTIMATION,
    }
    likely_tech = understanding.document_type in {
        DocumentType.SOFTWARE_ARCHITECTURE,
        DocumentType.API_SPECIFICATION,
        DocumentType.TECHNICAL_DOCUMENT,
    }

    # `has_meaningful_tables` is stricter than `has_tables`: a table-
    # heavy financial document has meaningful tables; a single
    # decorative layout table doesn't qualify. We approximate: tables
    # are meaningful when count >= 1 AND (the type wants them OR the
    # density is high — at least one table per ~30 text blocks).
    table_count = manifest.stats.tables if manifest else 0
    text_blocks = manifest.stats.text_blocks if manifest else 0
    type_wants_tables = (
        understanding.recommended_analysis_bias.prefer_table_enrichment
        or likely_finance
    )
    table_density_high = table_count > 0 and (
        text_blocks == 0 or table_count / max(text_blocks, 1) > 0.05
    )
    has_meaningful_tables = bool(table_count) and (
        type_wants_tables or table_density_high
    )

    # Images: meaningful when the parser flagged at least one as
    # diagram/chart, OR per-image triage assigned a non-decorative
    # role. Otherwise, images present but probably decorative (logos,
    # icons, repeated headers/footers).
    has_meaningful_images = False
    if manifest is not None:
        for item in manifest.items:
            if not item.type or item.type.lower() not in {
                "image", "figure", "diagram", "chart",
            }:
                continue
            meta = item.metadata or {}
            detected = str(meta.get("detected_type") or "").lower()
            role = str(meta.get("role") or "").lower()
            if detected in {"diagram", "chart", "figure"}:
                has_meaningful_images = True
                break
            if role and role not in {"logo", "decoration", "decorative"}:
                has_meaningful_images = True
                break
    if not has_meaningful_images and has_images:
        # Type-based signal: technical/presentation docs typically
        # have meaningful images.
        if likely_tech or understanding.document_type == DocumentType.PRESENTATION:
            has_meaningful_images = True

    return PostCompileSignals(
        has_clear_headings=has_headings,
        has_meaningful_tables=has_meaningful_tables,
        has_meaningful_images=has_meaningful_images,
        has_ocr_or_scanned_pages=scanned_signal,
        has_low_confidence_blocks=has_low_conf,
        likely_graph_candidate=likely_graph,
        likely_requirement_document=likely_req,
        likely_financial_document=likely_finance,
        likely_technical_document=likely_tech,
    )


# ---- Per-step recommendations ----------------------------------------


def _recommend_chunking(
    *,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
    manifest: ParsedContentManifest | None,
    digest: ContentDigest,
) -> ChunkingRecommendation:
    """Pick a chunking strategy based on heading structure + type."""
    settings: dict[str, object] = {
        "preserve_page_metadata": True,
        "preserve_heading_path": signals.has_clear_headings,
        "preserve_table_boundaries": signals.has_meaningful_tables,
    }

    if understanding.document_type == DocumentType.PRESENTATION:
        return ChunkingRecommendation(
            enabled=True,
            strategy=CHUNK_STRATEGY_PAGE_AWARE,
            reason="Slide deck — page-aware chunking preserves slide boundaries.",
            settings=settings,
        )

    if signals.has_clear_headings:
        return ChunkingRecommendation(
            enabled=True,
            strategy=CHUNK_STRATEGY_SECTION_AWARE,
            reason="Clear heading structure detected — section-aware chunking.",
            settings=settings,
        )

    # Page-aware fallback when pages are present but headings are weak.
    page_count = manifest.stats.page_count if manifest else None
    if page_count and page_count > 1:
        return ChunkingRecommendation(
            enabled=True,
            strategy=CHUNK_STRATEGY_PAGE_AWARE,
            reason="Weak heading signal but multi-page document — page-aware chunking.",
            settings=settings,
        )

    if (manifest and manifest.stats.text_blocks >= 100
            and not signals.has_clear_headings):
        return ChunkingRecommendation(
            enabled=True,
            strategy=CHUNK_STRATEGY_SEMANTIC,
            reason="Dense narrative without clear sections — semantic chunking.",
            settings=settings,
        )

    return ChunkingRecommendation(
        enabled=True,
        strategy=CHUNK_STRATEGY_FIXED_SIZE,
        reason="Simple uniform document — fixed-size chunking is sufficient.",
        settings=settings,
    )


def _recommend_steps(
    *,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
) -> list[StepRecommendation]:
    """One recommendation per non-chunking step."""
    out: list[StepRecommendation] = []

    bias = understanding.recommended_analysis_bias

    # ---- Table enrichment ------------------------------------------
    table_pages = _pages_with_item_type(manifest, "table")
    if signals.has_meaningful_tables:
        scope = (
            SCOPE_SELECTED_PAGES if table_pages and len(table_pages) <= 8
            else SCOPE_TABLES_ONLY
        )
        out.append(StepRecommendation(
            step=STEP_TABLE_ENRICHMENT,
            enabled=True,
            scope=scope,
            pages=tuple(sorted(table_pages))[:32],
            reason=(
                "Tables detected with meaningful surrounding text "
                f"({len(table_pages)} table-bearing pages)."
                if table_pages else
                "Document type/density indicates meaningful tables."
            ),
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_TABLE_ENRICHMENT,
            enabled=False,
            scope=SCOPE_NONE,
            reason=(
                "No tables in manifest." if not (manifest and manifest.stats.tables)
                else "Tables present but appear decorative; skipping."
            ),
        ))

    # ---- Vision enrichment -----------------------------------------
    image_pages = _pages_with_item_type(manifest, "image")
    if signals.has_ocr_or_scanned_pages:
        # Scanned content needs OCR/vision regardless of image count.
        scope = SCOPE_LOW_CONFIDENCE_PAGES
        pages = _pages_with_low_confidence(manifest)
        out.append(StepRecommendation(
            step=STEP_VISION_ENRICHMENT,
            enabled=True,
            scope=scope if pages else SCOPE_DOCUMENT,
            pages=tuple(sorted(pages))[:64],
            reason="Scanned/low-confidence content; vision needed for OCR fallback.",
        ))
    elif signals.has_meaningful_images:
        scope = (
            SCOPE_SELECTED_PAGES if image_pages and len(image_pages) <= 16
            else SCOPE_ALL_IMAGE_PAGES
        )
        out.append(StepRecommendation(
            step=STEP_VISION_ENRICHMENT,
            enabled=True,
            scope=scope,
            pages=tuple(sorted(image_pages))[:64],
            reason="Meaningful diagrams/charts detected.",
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_VISION_ENRICHMENT,
            enabled=False,
            scope=SCOPE_NONE,
            reason="No meaningful visual blocks; skipping vision LLM.",
        ))

    # ---- Image captioning ------------------------------------------
    if signals.has_meaningful_images:
        out.append(StepRecommendation(
            step=STEP_IMAGE_CAPTIONING,
            enabled=True,
            scope=SCOPE_SELECTED_IMAGES,
            reason="Caption meaningful diagrams/charts only — skip logos/decoration.",
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_IMAGE_CAPTIONING,
            enabled=False,
            scope=SCOPE_NONE,
            reason="No meaningful image content detected.",
        ))

    # ---- Requirement extraction ------------------------------------
    if bias.prefer_requirement_extraction:
        out.append(StepRecommendation(
            step=STEP_REQUIREMENT_EXTRACTION,
            enabled=True,
            scope=SCOPE_DOCUMENT,
            reason=(
                f"Document type {understanding.document_type.value} "
                "implies extractable requirements."
            ),
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_REQUIREMENT_EXTRACTION,
            enabled=False,
            scope=SCOPE_NONE,
            reason=(
                "Document type does not indicate explicit requirements."
            ),
        ))

    # ---- Risk extraction -------------------------------------------
    if bias.prefer_risk_extraction:
        out.append(StepRecommendation(
            step=STEP_RISK_EXTRACTION,
            enabled=True,
            scope=SCOPE_DOCUMENT,
            reason=(
                f"Document type {understanding.document_type.value} "
                "indicates a risk register / risk discussion."
            ),
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_RISK_EXTRACTION,
            enabled=False,
            scope=SCOPE_NONE,
            reason="Document type does not require risk analysis.",
        ))

    # ---- Quality assessment ----------------------------------------
    if signals.has_low_confidence_blocks or bias.prefer_quality_review:
        scope = (
            SCOPE_SELECTED_PAGES
            if signals.has_low_confidence_blocks
            else SCOPE_DOCUMENT
        )
        pages = _pages_with_low_confidence(manifest)
        out.append(StepRecommendation(
            step=STEP_QUALITY_ASSESSMENT,
            enabled=True,
            scope=scope,
            pages=tuple(sorted(pages))[:32],
            reason=(
                "Low parse confidence or scanned pages — run quality review."
                if signals.has_low_confidence_blocks
                else "Document type warrants quality review."
            ),
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_QUALITY_ASSESSMENT,
            enabled=False,
            scope=SCOPE_NONE,
            reason="Parse quality is good; no review trigger.",
        ))

    # ---- Graph extraction ------------------------------------------
    if signals.likely_graph_candidate:
        out.append(StepRecommendation(
            step=STEP_GRAPH_EXTRACTION,
            enabled=True,
            scope=SCOPE_DOCUMENT,
            reason=(
                f"Document type {understanding.document_type.value} "
                "contains useful entity relationships."
            ),
            candidate_entity_types=_candidate_entity_types(
                understanding.document_type,
            ),
        ))
    else:
        out.append(StepRecommendation(
            step=STEP_GRAPH_EXTRACTION,
            enabled=False,
            scope=SCOPE_NONE,
            reason="No strong entity-relationship structure detected.",
        ))

    # ---- Embedding -------------------------------------------------
    out.append(StepRecommendation(
        step=STEP_EMBEDDING,
        enabled=True,
        scope=SCOPE_DOCUMENT,
        reason="Required for retrieval.",
        model_profile="default_embedding",
    ))

    # ---- Indexing --------------------------------------------------
    out.append(StepRecommendation(
        step=STEP_INDEXING,
        enabled=True,
        scope=SCOPE_DOCUMENT,
        reason="Required for retrieval.",
    ))

    return out


def _pages_with_item_type(
    manifest: ParsedContentManifest | None, type_prefix: str,
) -> set[int]:
    """Return the set of page indices that contain at least one item
    whose `type` starts with `type_prefix` (case-insensitive)."""
    if manifest is None:
        return set()
    out: set[int] = set()
    for item in manifest.items:
        if not item.type or item.page_idx is None:
            continue
        if item.type.lower().startswith(type_prefix.lower()):
            out.add(item.page_idx)
    return out


def _pages_with_low_confidence(
    manifest: ParsedContentManifest | None,
) -> set[int]:
    """Pages where the parser flagged low confidence. Falls back to
    an empty set when the parser doesn't surface per-block confidence
    metadata."""
    if manifest is None:
        return set()
    out: set[int] = set()
    for item in manifest.items:
        if item.page_idx is None:
            continue
        meta = item.metadata or {}
        conf = meta.get("confidence") or meta.get("parse_confidence")
        if conf is None:
            continue
        try:
            score = float(conf)
        except (TypeError, ValueError):
            continue
        if score < 0.6:
            out.add(item.page_idx)
    return out


def _candidate_entity_types(document_type: DocumentType) -> tuple[str, ...]:
    """Type-aware seed list for graph extraction. The graph builder
    uses these as a hint when the deployment supports it; producers
    that don't read it ignore the field."""
    return {
        DocumentType.SOFTWARE_ARCHITECTURE: (
            "system", "service", "component", "module", "actor",
            "external_dependency",
        ),
        DocumentType.API_SPECIFICATION: (
            "endpoint", "resource", "schema", "method",
        ),
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION: (
            "requirement", "actor", "feature", "constraint",
        ),
        DocumentType.PROJECT_PLAN: (
            "phase", "milestone", "owner", "risk",
        ),
        DocumentType.POLICY: (
            "obligation", "responsible_party", "scope",
        ),
        DocumentType.PROCEDURE: (
            "step", "actor", "input", "output",
        ),
    }.get(document_type, ())


# ---- Profile selection ------------------------------------------------


def _pick_profile(
    *,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
) -> str:
    if signals.has_low_confidence_blocks and (
        manifest and (manifest.stats.parse_quality_score or 1.0) < 0.4
    ):
        return PROFILE_DIAGNOSTIC

    high_value_types = {
        DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION,
        DocumentType.BUSINESS_REQUIREMENT,
        DocumentType.SOFTWARE_ARCHITECTURE,
        DocumentType.PROPOSAL,
        DocumentType.ESTIMATION,
        DocumentType.CONTRACT,
        DocumentType.LEGAL_DOCUMENT,
        DocumentType.PROJECT_PLAN,
    }
    if understanding.document_type in high_value_types:
        return PROFILE_PREMIUM

    if signals.has_meaningful_tables or signals.has_meaningful_images:
        return PROFILE_BALANCED

    if (
        understanding.document_type == DocumentType.UNKNOWN
        and (understanding.document_type_confidence < 0.5)
    ):
        # Stay conservative for unknown docs.
        return PROFILE_FAST

    if not signals.has_meaningful_tables and not signals.has_meaningful_images:
        return PROFILE_FAST

    return PROFILE_BALANCED


def _confidence_for_assessment(
    *,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
    manifest: ParsedContentManifest | None,
) -> float:
    base = 0.5 + (understanding.document_type_confidence * 0.4)
    if not signals.has_clear_headings:
        base -= 0.05
    if signals.has_low_confidence_blocks:
        base -= 0.1
    if signals.likely_graph_candidate or signals.likely_requirement_document:
        base += 0.05
    if manifest is None:
        base -= 0.1
    return max(0.0, min(1.0, base))


def _estimate_resources(
    steps: list[StepRecommendation], signals: PostCompileSignals,
) -> tuple[str, str]:
    """Coarse time/cost buckets — used for the FE Planning Report."""
    enabled = [s for s in steps if s.enabled]
    expensive_steps = {
        STEP_VISION_ENRICHMENT,
        STEP_GRAPH_EXTRACTION,
        STEP_REQUIREMENT_EXTRACTION,
        STEP_RISK_EXTRACTION,
        STEP_QUALITY_ASSESSMENT,
    }
    expensive_count = sum(1 for s in enabled if s.step in expensive_steps)
    if expensive_count >= 3 or signals.has_ocr_or_scanned_pages:
        return "high", "high"
    if expensive_count >= 1:
        return "medium", "medium"
    return "low", "low"


# ---- Reporting --------------------------------------------------------


def _build_content_report(
    *,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
    signals: PostCompileSignals,
    digest: ContentDigest,
) -> ContentReport:
    page_count = manifest.stats.page_count if manifest else (
        profile.page_count if profile else None
    )
    structure = (
        "excellent" if signals.has_clear_headings and len(digest.heading_outline) >= 8
        else "good" if signals.has_clear_headings
        else "fair" if (manifest and manifest.stats.text_blocks)
        else "poor"
    )
    layout_score = (
        manifest.stats.layout_complexity_score
        if manifest and manifest.stats.layout_complexity_score is not None
        else (profile.layout_complexity_score
              if profile and profile.layout_complexity_score is not None
              else 0.3)
    )
    layout = (
        "high" if layout_score > 0.7
        else "medium" if layout_score > 0.4
        else "low"
    )
    text_blocks = manifest.stats.text_blocks if manifest else 0
    density = (
        "high" if text_blocks > 200
        else "medium" if text_blocks > 50
        else "low"
    )

    observations: list[str] = []
    if signals.has_meaningful_tables:
        observations.append("Document carries meaningful tables.")
    if signals.has_meaningful_images:
        observations.append("Document carries diagrams or charts.")
    if signals.has_ocr_or_scanned_pages:
        observations.append("Document contains scanned / low-extractable pages.")

    return ContentReport(
        language=getattr(profile, "language", None) if profile else None,
        page_count=page_count,
        structure_quality=structure,
        layout_complexity=layout,
        content_density=density,
        has_clear_sections=signals.has_clear_headings,
        has_tables=bool(manifest and manifest.stats.tables),
        has_images=bool(manifest and manifest.stats.images),
        has_formulas=bool(manifest and manifest.stats.equations),
        has_ocr_pages=signals.has_ocr_or_scanned_pages,
        important_observations=tuple(observations),
    )


def _build_quality_report(
    *,
    manifest: ParsedContentManifest | None,
    profile: DocumentProfile | None,
    signals: PostCompileSignals,
) -> QualityReport:
    parse_quality = (
        manifest.stats.parse_quality_score if manifest else None
    )
    parse_confidence = (
        "high" if parse_quality is None or parse_quality >= 0.8
        else "medium" if parse_quality >= 0.5
        else "low"
    )
    risk = (
        "high" if signals.has_ocr_or_scanned_pages and parse_quality is not None and parse_quality < 0.5
        else "medium" if signals.has_low_confidence_blocks
        else "low"
    )

    issues: list[QualityIssue] = []
    if signals.has_ocr_or_scanned_pages:
        issues.append(QualityIssue(
            issue="Scanned or low-extractable content present.",
            severity="medium",
            affected_pages=tuple(sorted(_pages_with_low_confidence(manifest)))[:32],
            recommendation="Run OCR / vision enrichment for the affected pages.",
        ))
    if parse_quality is not None and parse_quality < 0.5:
        issues.append(QualityIssue(
            issue="Parser reported low overall quality.",
            severity="high",
            affected_pages=(),
            recommendation="Manual review recommended.",
        ))

    review_required = (
        bool(issues)
        and (parse_quality is None or parse_quality < 0.5)
    )
    candidates: list[ReviewCandidate] = []
    for page in sorted(_pages_with_low_confidence(manifest))[:16]:
        candidates.append(ReviewCandidate(
            page=page,
            reason="Low parse confidence",
        ))

    return QualityReport(
        parse_confidence=parse_confidence,
        risk_level=risk,
        detected_issues=tuple(issues),
        manual_review_required=review_required,
        manual_review_candidates=tuple(candidates),
    )


def _build_main_reasoning(
    *,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
    profile_label: str,
) -> list[str]:
    bits: list[str] = []
    bits.append(
        f"Detected document type: {understanding.document_type.value} "
        f"(confidence {understanding.document_type_confidence:.2f})."
    )
    bits.append(
        f"Selected profile: {profile_label}."
    )
    if understanding.recommended_analysis_bias.reason:
        bits.append(understanding.recommended_analysis_bias.reason)
    if signals.has_meaningful_tables:
        bits.append("Tables are meaningful — enable table enrichment.")
    if signals.has_meaningful_images:
        bits.append("Diagrams / charts present — enable selective vision.")
    if signals.has_ocr_or_scanned_pages:
        bits.append("Scanned content — vision/OCR required for those pages.")
    if signals.likely_graph_candidate:
        bits.append("Document type implies useful entity relationships — graph enabled.")
    return bits


def _overall_assessment(
    *,
    profile_label: str,
    understanding: DocumentUnderstanding,
    signals: PostCompileSignals,
) -> str:
    if profile_label == PROFILE_DIAGNOSTIC:
        return (
            "Diagnostic mode: parse quality is poor — recommend manual review "
            "before relying on this document."
        )
    if profile_label == PROFILE_PREMIUM:
        return (
            f"High-value {understanding.document_type.value}: applying "
            "premium enrichment with full extraction support."
        )
    if profile_label == PROFILE_FAST:
        return (
            f"Lightweight document ({understanding.document_type.value}): "
            "fast profile with chunking + embedding only."
        )
    return (
        f"Balanced ingestion for {understanding.document_type.value}: "
        "selective enrichment based on detected signals."
    )
