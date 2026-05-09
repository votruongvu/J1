"""Top-level post-compile planning orchestration.

Composes the deterministic core (Document Understanding + Content
Digest + Rule-based Post-Compile Assessment) into a
`PlanningResult` ready for persistence as `planning_result.json`.

This module is the boundary the workflow / activity layer calls into.
Pure function — no I/O, no Temporal coupling — so unit tests can
exercise the full pipeline without spinning a workflow.

The optional LLM-assisted path is kept here as a callable injection
point (`llm_planner`) rather than baked in: the activity layer
provides the actual LLM call; this module just describes how the
LLM result merges with the rule-based plan, validates the merge,
and falls back when the LLM fails."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from dataclasses import dataclass

from j1.domains import (
    DOMAIN_GENERAL,
    DomainContext,
    DomainPack,
    DomainPlanningOverlay,
    DomainRegistry,
)
from j1.domains.registry import select_domain
from j1.processing.content_digest import ContentDigest, build_content_digest
from j1.processing.document_understanding import (
    DocumentMetadata,
    DocumentUnderstanding,
    assess_document_understanding,
)
from j1.processing.manifest import (
    ParsedContentItem,
    ParsedContentManifest,
)
from j1.processing.planning_result import (
    PLANNING_RESULT_SCHEMA_VERSION,
    PLANNING_SOURCE_LLM,
    PLANNING_SOURCE_RULE_BASED,
    PLANNING_SOURCE_RULE_BASED_FALLBACK,
    PlanningResult,
    PlanningValidationError,
    assessment_to_planning_result,
    validate_planning_result_dict,
)
from j1.processing.planning_settings import PlanningSettings
from j1.processing.post_compile_assessment import (
    PostCompileAssessment,
    build_post_compile_assessment,
)
from j1.processing.profiling import DocumentProfile


__all__ = [
    "PlanningContext",
    "build_planning_context",
    "build_planning_result",
]


_log = logging.getLogger("j1.planning")


# Type alias — the LLM planner is a callable that takes a planning
# context dict and returns a parsed JSON dict (or raises on failure).
# Keeping it injection-shaped lets the unit tests substitute a stub
# while production wires the real LLM client.
LLMPlanner = Callable[[dict[str, Any]], dict[str, Any]]


# ---- Planning context -------------------------------------------------


class PlanningContext(dict):
    """The compact JSON-shaped payload fed to the LLM planner.

    Inherits from `dict` so it serialises straight to JSON without
    bespoke encoders. Construction goes through
    `build_planning_context()` which enforces the privacy caps.
    """


def build_planning_context(
    *,
    run_id: str,
    document: DocumentMetadata,
    file_size_bytes: int | None,
    profile: DocumentProfile | None,
    manifest: ParsedContentManifest | None,
    digest: ContentDigest,
    understanding: DocumentUnderstanding,
    rule_based: PostCompileAssessment,
) -> PlanningContext:
    """Build the compact planning context the LLM planner sees.

    No raw document content. No full-page text. No full chunk lists.
    The digest's caps are already applied; this function just shapes
    the wire payload."""
    stats = manifest.stats if manifest else None
    file_size_mb = (
        round(file_size_bytes / (1024 * 1024), 3)
        if file_size_bytes is not None else None
    )
    return PlanningContext({
        "run_id": run_id,
        "document": {
            "document_id": document.document_id,
            "filename": document.filename,
            "mime_type": document.mime_type,
            "page_count": stats.page_count if stats else None,
            "language": document.language,
            "parse_backend": manifest.parse_method if manifest else None,
            "compiler_kind": manifest.parser if manifest else None,
            "file_size_mb": file_size_mb,
        },
        "initial_profile": _profile_to_payload(profile),
        "content_inventory": _stats_to_payload(stats),
        "content_digest": _digest_to_payload(digest),
        "document_understanding": _understanding_to_payload(understanding),
        "page_inventory": _page_inventory_payload(manifest),
        "rule_based_assessment": _rule_based_assessment_payload(rule_based),
    })


# ---- Top-level builder ------------------------------------------------


def build_planning_result(
    *,
    run_id: str,
    document: DocumentMetadata,
    file_size_bytes: int | None,
    profile: DocumentProfile | None,
    manifest: ParsedContentManifest | None,
    settings: PlanningSettings,
    llm_planner: LLMPlanner | None = None,
    now: datetime | None = None,
    domain_registry: DomainRegistry | None = None,
    domain_override: str | None = None,
    workspace_default_domain: str | None = None,
) -> PlanningResult:
    """Build the full `PlanningResult` from compile outputs.

    Pure function — caller injects the LLM planner and the domain
    registry; we don't bind a transport here. Returns a result whose
    `source` field is one of:
      * `rule_based` — LLM disabled or no planner provided.
      * `llm` — LLM ran and its output validated.
      * `rule_based_fallback` — LLM ran but failed validation /
        threw / produced unsafe output and `fail_open=True`.

    Domain selection runs after the generic Document Understanding +
    rule-based assessment. When a non-generic pack wins (auto-detect
    confidence ≥ threshold OR an operator override), its overlay is
    applied on top of the rule-based plan; otherwise the generic
    decisions stand. The chosen domain is recorded on the result's
    `domain_context` either way.

    Raises `PlanningValidationError` when LLM output fails validation
    AND `settings.fail_open=False`.
    """
    timestamp = (now or datetime.now(timezone.utc)).isoformat()

    understanding = assess_document_understanding(
        metadata=document,
        manifest=manifest,
        max_early_pages=settings.max_early_pages,
    )
    digest = build_content_digest(
        manifest=manifest,
        understanding=understanding,
        max_sample_blocks=settings.max_sample_blocks,
        max_preview_chars=settings.max_preview_chars,
        max_early_pages=settings.max_early_pages,
    )
    rule_based = build_post_compile_assessment(
        understanding=understanding,
        manifest=manifest,
        profile=profile,
        digest=digest,
    )

    # Resolve the domain pack (or fall back to generic).
    domain_context, domain_pack = _resolve_domain(
        registry=domain_registry,
        settings=settings,
        document=document,
        understanding=understanding,
        manifest=manifest,
        digest=digest,
        domain_override=domain_override,
        workspace_default_domain=workspace_default_domain,
    )

    # Apply the matching domain-pack overlay (when one was
    # selected). Generic returns the rule-based plan unchanged.
    rule_based_with_domain, applied_rule_ids = _apply_domain_overlay(
        rule_based=rule_based,
        domain_context=domain_context,
        pack=domain_pack,
    )
    if applied_rule_ids:
        domain_context = _attach_applied_rules(
            domain_context, applied_rule_ids,
        )

    rule_based_result = assessment_to_planning_result(
        run_id=run_id,
        document_id=document.document_id,
        created_at=timestamp,
        assessment=rule_based_with_domain,
        source=PLANNING_SOURCE_RULE_BASED,
    )
    rule_based_result = _with_domain_context(
        rule_based_result, domain_context,
    )
    rule_based_result = _replace_dataclass(
        rule_based_result,
        planner_mode=_planner_mode_for(
            settings=settings,
            source=PLANNING_SOURCE_RULE_BASED,
        ),
    )

    # No LLM path? Done.
    if not settings.llm_planning_enabled or llm_planner is None:
        return rule_based_result

    context = build_planning_context(
        run_id=run_id,
        document=document,
        file_size_bytes=file_size_bytes,
        profile=profile,
        manifest=manifest,
        digest=digest,
        understanding=understanding,
        rule_based=rule_based,
    )

    try:
        llm_payload = llm_planner(dict(context))
    except Exception as exc:  # noqa: BLE001 — caller-supplied LLM may raise anything
        _log.warning(
            "LLM planner failed for run=%s document=%s: %s",
            run_id, document.document_id, exc,
        )
        if not settings.fail_open:
            raise PlanningValidationError(
                f"LLM planner failed and fail_open=False: {exc}"
            ) from exc
        return _to_fallback(rule_based_result, reason=f"LLM call failed: {exc}")

    page_count = manifest.stats.page_count if manifest else None
    try:
        validate_planning_result_dict(llm_payload, page_count=page_count)
    except PlanningValidationError as exc:
        _log.warning(
            "LLM planner output failed validation for run=%s: %s",
            run_id, exc,
        )
        if not settings.fail_open:
            raise
        return _to_fallback(rule_based_result, reason=f"LLM output invalid: {exc}")

    merged = _merge_llm_into_rule_based(
        rule_based_result=rule_based_result,
        llm_payload=llm_payload,
        timestamp=timestamp,
    )
    return _replace_dataclass(
        merged,
        planner_mode=_planner_mode_for(
            settings=settings,
            source=PLANNING_SOURCE_LLM,
        ),
    )


# ---- Helpers ----------------------------------------------------------


def _to_fallback(rule: PlanningResult, *, reason: str) -> PlanningResult:
    """Promote the rule-based result to a fallback PlanningResult."""
    warnings = list(rule.warnings)
    warnings.append(f"LLM-assisted planning unavailable; falling back. {reason}")
    return PlanningResult(
        run_id=rule.run_id,
        document_id=rule.document_id,
        planning_version=rule.planning_version,
        planning_phase=rule.planning_phase,
        source=PLANNING_SOURCE_RULE_BASED_FALLBACK,
        created_at=rule.created_at,
        recommended_profile=rule.recommended_profile,
        confidence=rule.confidence,
        document_understanding=rule.document_understanding,
        decision_summary=rule.decision_summary,
        content_report=rule.content_report,
        quality_report=rule.quality_report,
        execution_plan=rule.execution_plan,
        rule_based_assessment=rule.rule_based_assessment,
        rule_based_comparison=rule.rule_based_comparison,
        warnings=warnings,
        next_actions=list(rule.next_actions),
        domain_context=dict(rule.domain_context),
        planner_mode=PLANNING_SOURCE_RULE_BASED_FALLBACK,
    )


def _planner_mode_for(*, settings: PlanningSettings, source: str) -> str:
    """Project the (settings.plan_mode, source) pair onto the wire-
    facing `plannerMode` vocabulary the FE renders.

    `settings.plan_mode` is the operator's intent (`rule_based` /
    `llm` / `hybrid`); `source` is the actual outcome
    (`rule_based` / `llm` / `rule_based_fallback`). The wire field
    summarises both so the FE never has to derive it."""
    if source == PLANNING_SOURCE_RULE_BASED_FALLBACK:
        return PLANNING_SOURCE_RULE_BASED_FALLBACK
    if source == PLANNING_SOURCE_LLM:
        # Hybrid is operationally LLM with rule-based as the safety
        # net — surface the operator's intent so the badge reads
        # "Hybrid" when the deployment opted in.
        if settings.plan_mode == "hybrid":
            return "hybrid"
        return "llm"
    # Pure rule-based outcome.
    return "rule_based"


def _merge_llm_into_rule_based(
    *,
    rule_based_result: PlanningResult,
    llm_payload: dict[str, Any],
    timestamp: str,
) -> PlanningResult:
    """Adopt the LLM planner's decisions while preserving the rule-
    based result for comparison.

    LLM wins on top-level decisions (profile, plan, document
    understanding, reports). Rule-based stays accessible via
    `rule_based_assessment`. The `rule_based_comparison` field is
    populated with what the LLM accepted vs. overrode."""
    comparison = _build_rule_based_comparison(
        rule_based_payload=rule_based_result.execution_plan,
        llm_payload=(llm_payload.get("execution_plan") or {}),
    )

    return PlanningResult(
        run_id=rule_based_result.run_id,
        document_id=rule_based_result.document_id,
        planning_version=str(
            llm_payload.get("planning_version") or PLANNING_RESULT_SCHEMA_VERSION
        ),
        planning_phase="post_compile",
        source=PLANNING_SOURCE_LLM,
        created_at=timestamp,
        recommended_profile=str(llm_payload.get("recommended_profile")),
        confidence=float(llm_payload.get("confidence") or 0.0),
        document_understanding=dict(llm_payload.get("document_understanding") or {}),
        decision_summary=dict(llm_payload.get("decision_summary") or {}),
        content_report=dict(llm_payload.get("content_report") or {}),
        quality_report=dict(llm_payload.get("quality_report") or {}),
        execution_plan=dict(llm_payload.get("execution_plan") or {}),
        rule_based_assessment=dict(rule_based_result.rule_based_assessment),
        rule_based_comparison=comparison,
        warnings=[
            *rule_based_result.warnings,
            *[str(w) for w in (llm_payload.get("warnings") or []) if w],
        ],
        next_actions=[
            str(a) for a in (llm_payload.get("next_actions") or []) if a
        ] or list(rule_based_result.next_actions),
        # LLM payload may also carry a `domain_context` block when
        # the prompt addon told it to. Prefer that; fall back to the
        # rule-based context.
        domain_context=dict(
            llm_payload.get("domain_context")
            or rule_based_result.domain_context
        ),
    )


def _build_rule_based_comparison(
    *,
    rule_based_payload: dict[str, Any],
    llm_payload: dict[str, Any],
) -> dict[str, Any]:
    """Diff the LLM plan against the rule-based plan at step level.

    Coarse intent: surface "the LLM agreed with the rule-based
    decision" vs. "the LLM overrode it" so reviewers can audit. We
    don't deep-compare every field — only the `enabled` flag, since
    that's the operationally consequential bit."""
    rb_steps = (rule_based_payload.get("steps") or {}) if isinstance(rule_based_payload.get("steps"), dict) else {}
    llm_steps = (llm_payload.get("steps") or {}) if isinstance(llm_payload.get("steps"), dict) else {}

    accepted: list[str] = []
    overridden: list[dict[str, Any]] = []
    for step_name, rb_entry in rb_steps.items():
        if not isinstance(rb_entry, dict):
            continue
        llm_entry = llm_steps.get(step_name)
        if not isinstance(llm_entry, dict):
            accepted.append(step_name)
            continue
        rb_enabled = bool(rb_entry.get("enabled"))
        llm_enabled = bool(llm_entry.get("enabled"))
        if rb_enabled == llm_enabled:
            accepted.append(step_name)
        else:
            overridden.append({
                "rule": step_name,
                "original_recommendation": "enabled" if rb_enabled else "skipped",
                "llm_recommendation": "enabled" if llm_enabled else "skipped",
                "reason": str(llm_entry.get("reason") or ""),
            })
    return {
        "accepted_rule_recommendations": accepted,
        "overridden_rule_recommendations": overridden,
    }


# ---- Payload builders -------------------------------------------------


def _profile_to_payload(profile: DocumentProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "extension": profile.extension,
        "text_extractable_ratio": profile.text_extractable_ratio,
        "has_images": profile.has_images,
        "has_tables": profile.has_tables,
        "has_scanned_pages": profile.has_scanned_pages,
        "parse_quality_score": profile.parse_quality_score,
        "text_sufficiency_score": profile.text_sufficiency_score,
        "layout_complexity_score": profile.layout_complexity_score,
    }


def _stats_to_payload(stats) -> dict[str, Any]:
    if stats is None:
        return {}
    return {
        "total_blocks": stats.total_items,
        "headings": None,  # not separately counted by the manifest today
        "paragraphs": stats.text_blocks,
        "tables": stats.tables,
        "images": stats.images,
        "formulas": stats.equations,
        "code_blocks": None,
        "footnotes": None,
        "ocr_pages": stats.scanned_pages,
        "low_confidence_blocks": None,
    }


def _digest_to_payload(digest: ContentDigest) -> dict[str, Any]:
    return {
        "summary": digest.summary,
        "title_candidates": [
            {
                "source": c.source,
                "text": c.text,
                "page": c.page,
                "score": c.score,
            }
            for c in digest.title_candidates
        ],
        "heading_outline": [
            {"level": level, "text": text, "page": page}
            for level, text, page in digest.heading_outline
        ],
        "early_page_digest": [
            {
                "page": p.page,
                "headings": list(p.headings),
                "paragraph_previews": list(p.paragraph_previews),
                "table_hints": list(p.table_hints),
                "image_hints": list(p.image_hints),
            }
            for p in digest.early_page_digest
        ],
        "sample_text_blocks": [
            {"page": b.page, "type": b.type, "preview": b.preview}
            for b in digest.sample_text_blocks
        ],
        "sample_tables": [
            {
                "page": t.page,
                "row_count": t.row_count,
                "column_count": t.column_count,
                "preview": t.preview,
            }
            for t in digest.sample_tables
        ],
        "sample_images": [
            {
                "page": i.page,
                "detected_type": i.detected_type,
                "nearby_text": i.nearby_text,
            }
            for i in digest.sample_images
        ],
    }


def _understanding_to_payload(u: DocumentUnderstanding) -> dict[str, Any]:
    return {
        "title_source": u.title_source,
        "detected_title": u.detected_title,
        "title_quality": u.title_quality,
        "document_type": u.document_type.value,
        "document_type_confidence": u.document_type_confidence,
        "business_domain": u.business_domain,
        "primary_topic": u.primary_topic,
        "document_purpose": u.document_purpose,
        "intended_audience": u.intended_audience,
        "document_importance": u.document_importance,
        "expected_information_types": list(u.expected_information_types),
        "recommended_analysis_bias": {
            "prefer_requirement_extraction": u.recommended_analysis_bias.prefer_requirement_extraction,
            "prefer_risk_extraction": u.recommended_analysis_bias.prefer_risk_extraction,
            "prefer_table_enrichment": u.recommended_analysis_bias.prefer_table_enrichment,
            "prefer_graph_extraction": u.recommended_analysis_bias.prefer_graph_extraction,
            "prefer_visual_enrichment": u.recommended_analysis_bias.prefer_visual_enrichment,
            "prefer_quality_review": u.recommended_analysis_bias.prefer_quality_review,
            "reason": u.recommended_analysis_bias.reason,
        },
    }


def _page_inventory_payload(
    manifest: ParsedContentManifest | None,
) -> list[dict[str, Any]]:
    if manifest is None or not manifest.items:
        return []
    pages: dict[int, dict[str, Any]] = {}
    for item in manifest.items:
        page = item.page_idx
        if page is None:
            continue
        entry = pages.setdefault(page, {
            "page": page,
            "block_count": 0,
            "block_types": set(),
            "quality": "good",
            "has_table": False,
            "has_image": False,
            "requires_attention": False,
        })
        entry["block_count"] += 1
        if item.type:
            entry["block_types"].add(item.type.lower())
        item_type_lower = (item.type or "").lower()
        if item_type_lower.startswith("table"):
            entry["has_table"] = True
        if item_type_lower in {"image", "figure", "diagram", "chart"}:
            entry["has_image"] = True
        meta = item.metadata or {}
        conf = meta.get("confidence") or meta.get("parse_confidence")
        try:
            score = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None and score < 0.6:
            entry["quality"] = "fair"
            entry["requires_attention"] = True
    out = []
    for page in sorted(pages):
        entry = pages[page]
        entry["block_types"] = sorted(entry["block_types"])
        out.append(entry)
    return out


# ---- Domain pack integration ----------------------------------------


@dataclass(frozen=True)
class _PlannerDetectionContext:
    """Lightweight `DetectionContext` that satisfies the protocol
    declared in `j1.domains.models`. Built fresh per planning call —
    no caching, the per-document inputs are what matter."""

    title: str
    title_quality: str
    filename: str | None
    early_page_text: str
    heading_outline: tuple[tuple[int, str, int | None], ...]
    table_captions: tuple[str, ...]
    image_captions: tuple[str, ...]
    document_type_hint: str | None
    table_header_rows: tuple[tuple[str, ...], ...]


def _build_detection_context(
    *,
    document: DocumentMetadata,
    understanding: DocumentUnderstanding,
    manifest: ParsedContentManifest | None,
    digest: ContentDigest,
) -> _PlannerDetectionContext:
    """Project everything the post-compile planner already has into
    the shape domain detectors consume."""
    table_captions: list[str] = []
    image_captions: list[str] = []
    table_header_rows: list[tuple[str, ...]] = []
    if manifest is not None:
        for item in manifest.items:
            kind = (item.type or "").lower()
            if kind.startswith("table"):
                if item.caption:
                    table_captions.append(item.caption)
                if item.text_preview:
                    # Table preview often = header row joined by `|`
                    # or `,`; split it back into cells so detectors
                    # can match BOQ-shaped rows.
                    cells = _split_header_row(item.text_preview)
                    if cells:
                        table_header_rows.append(cells)
            elif kind in {"image", "figure", "diagram", "chart"}:
                if item.caption:
                    image_captions.append(item.caption)

    early_page_text = " ".join(
        " ".join(p.paragraph_previews)
        for p in digest.early_page_digest
    )
    return _PlannerDetectionContext(
        title=understanding.detected_title or "",
        title_quality=understanding.title_quality,
        filename=document.filename,
        early_page_text=early_page_text,
        heading_outline=digest.heading_outline,
        table_captions=tuple(table_captions),
        image_captions=tuple(image_captions),
        document_type_hint=understanding.document_type.value,
        table_header_rows=tuple(table_header_rows),
    )


def _split_header_row(preview: str) -> tuple[str, ...]:
    """Split a table preview into header cells. The bridge surfaces
    table previews as `'col1 | col2 | col3'` for plaintext or
    `'<th>col1</th>...'` for HTML; handle both shapes."""
    if not preview:
        return ()
    raw = preview
    # Quick HTML strip — looking only for header-like cells.
    raw = raw.replace("</th>", "|").replace("<th>", "")
    raw = raw.replace("</td>", "|").replace("<td>", "")
    candidates = [c.strip() for c in raw.split("|") if c.strip()]
    # Header-row heuristic: short cells, not too many of them.
    if 2 <= len(candidates) <= 12 and all(
        len(c) <= 32 for c in candidates
    ):
        return tuple(candidates[:12])
    return ()


def _resolve_domain(
    *,
    registry: DomainRegistry | None,
    settings: PlanningSettings,
    document: DocumentMetadata,
    understanding: DocumentUnderstanding,
    manifest: ParsedContentManifest | None,
    digest: ContentDigest,
    domain_override: str | None,
    workspace_default_domain: str | None,
) -> tuple[DomainContext, DomainPack | None]:
    """Run domain selection and return `(context, pack)`.

    `pack` is None when `general` wins (legacy planner stays in
    charge). The context always represents the chosen state — even
    a generic-fallback run gets a populated `domain_context`."""
    if not settings.domain_packs_enabled or registry is None:
        return _generic_fallback_context(reason=(
            "Domain packs disabled by configuration."
            if not settings.domain_packs_enabled
            else "No domain registry wired."
        )), None

    detection_context = _build_detection_context(
        document=document,
        understanding=understanding,
        manifest=manifest,
        digest=digest,
    )
    context = select_domain(
        registry=registry,
        detection_context=detection_context,
        user_override=domain_override,
        workspace_default=(
            workspace_default_domain
            or settings.workspace_default_domain
        ),
        detection_enabled=settings.domain_detection_enabled,
        detection_threshold=settings.domain_detection_min_confidence,
        allowed_overrides=frozenset(settings.allowed_domain_overrides),
    )
    pack = (
        registry.get(context.selected_domain)
        if context.selected_domain != DOMAIN_GENERAL else None
    )
    return context, pack


def _generic_fallback_context(*, reason: str) -> DomainContext:
    return DomainContext(
        selected_domain=DOMAIN_GENERAL,
        selection_source="fallback_general",
        confidence=0.0,
        domain_pack_version="generic",
        warnings=(reason,) if reason else (),
    )


def _apply_domain_overlay(
    *,
    rule_based: PostCompileAssessment,
    domain_context: DomainContext,
    pack: DomainPack | None,
) -> tuple[PostCompileAssessment, list[str]]:
    """Layer the matching domain overlay on top of the rule-based
    assessment.

    Returns `(updated_assessment, applied_rule_ids)`. When no pack
    matches (or no overlay exists for the detected type), returns
    the input unchanged + empty list.
    """
    if pack is None:
        return rule_based, []

    detected_type = _detected_type_from_context(domain_context)
    overlay: DomainPlanningOverlay | None = None
    if detected_type:
        overlay = pack.overlays.get(detected_type)
    if overlay is None:
        return rule_based, []

    applied = [overlay.applied_rule_id] if overlay.applied_rule_id else []

    # Project recommended_profile + chunking onto the assessment.
    new_profile = (
        overlay.recommended_profile or rule_based.recommended_profile
    )
    chunking = rule_based.execution_plan.chunking
    if overlay.chunking_strategy and overlay.chunking_strategy != chunking.strategy:
        chunking = _replace_dataclass(
            chunking,
            strategy=overlay.chunking_strategy,
            reason=(
                f"Domain pack {pack.id} requires {overlay.chunking_strategy} "
                f"chunking for {detected_type}."
            ),
        )

    # Apply step overrides — replace the matching `StepRecommendation`
    # in the rule-based plan when an overlay carries the step.
    new_steps = list(rule_based.execution_plan.steps)
    for idx, step in enumerate(new_steps):
        override = overlay.step_overrides.get(step.step)
        if not override:
            continue
        new_steps[idx] = _step_with_override(step, override, pack=pack)
        applied.append(
            f"{pack.id}.plan.{detected_type}.{step.step}"
        )

    new_plan = _replace_dataclass(
        rule_based.execution_plan,
        chunking=chunking,
        steps=tuple(new_steps),
    )

    new_assessment = _replace_dataclass(
        rule_based,
        recommended_profile=new_profile,
        execution_plan=new_plan,
    )
    return new_assessment, applied


def _step_with_override(step, override: dict[str, Any], *, pack: DomainPack):
    """Return a copy of `step` updated by the overlay dict."""
    enabled = bool(override.get("enabled", step.enabled))
    scope = str(
        override.get("scope") or step.scope or ("document" if enabled else "none")
    )
    reason = str(
        override.get("reason") or step.reason
        or f"Domain pack {pack.id} adjusted this step."
    )
    pages = override.get("pages")
    if pages is None:
        pages_tuple = step.pages
    else:
        pages_tuple = tuple(int(p) for p in pages if p is not None)
    candidate_entity_types = override.get("candidate_entity_types")
    return _replace_dataclass(
        step,
        enabled=enabled,
        scope=scope,
        reason=reason,
        pages=pages_tuple,
        candidate_entity_types=(
            tuple(str(t) for t in candidate_entity_types)
            if candidate_entity_types is not None
            else step.candidate_entity_types
        ),
    )


def _detected_type_from_context(context: DomainContext) -> str | None:
    """Find the detected document_type from the candidate that won."""
    for cand in context.candidates:
        if cand.domain_id == context.selected_domain and cand.detected_document_type:
            return cand.detected_document_type
    return None


def _attach_applied_rules(
    context: DomainContext, applied: list[str],
) -> DomainContext:
    """Append overlay-derived rule ids to the context's
    `applied_domain_rules` list, preserving order + deduping."""
    seen: set[str] = set(context.applied_domain_rules)
    out: list[str] = list(context.applied_domain_rules)
    for rule_id in applied:
        if rule_id and rule_id not in seen:
            seen.add(rule_id)
            out.append(rule_id)
    return _replace_dataclass(context, applied_domain_rules=tuple(out))


def _with_domain_context(
    result: PlanningResult, context: DomainContext,
) -> PlanningResult:
    """Attach the domain context to a `PlanningResult` and project
    the pack's detected document_type into document_understanding.

    When the pack detected a domain-specific type (e.g. `boq`,
    `inspection_report` from a construction pack) we overwrite the
    generic detector's `document_type` so the FE Planning Report
    and downstream consumers see the more specific value. The
    generic type stays accessible via `rule_based_assessment` if
    reviewers need to diff."""
    detected = _detected_type_from_context(context)
    understanding = dict(result.document_understanding or {})
    if detected:
        understanding["document_type"] = detected
        # Confidence: take the domain pack's confidence as the
        # type-detection confidence — the generic detector's
        # value applied to the generic taxonomy, not this one.
        understanding["document_type_confidence"] = context.confidence
    return _replace_dataclass(
        result,
        document_understanding=understanding,
        domain_context=context.to_dict(),
    )


def _replace_dataclass(obj, **kwargs):
    """Frozen-dataclass-friendly replacement helper. Used in lieu of
    `dataclasses.replace` so we don't have to import it at every
    call site."""
    from dataclasses import replace
    return replace(obj, **kwargs)


# ---- Existing helpers continue below --------------------------------


def _rule_based_assessment_payload(
    a: PostCompileAssessment,
) -> dict[str, Any]:
    """Compact projection of the rule-based assessment used inside the
    planning context fed to the LLM. Carries decisions + reasons; no
    raw document content."""
    return {
        "recommended_profile": a.recommended_profile,
        "confidence": a.confidence,
        "signals": {
            "has_clear_headings": a.signals.has_clear_headings,
            "has_meaningful_tables": a.signals.has_meaningful_tables,
            "has_meaningful_images": a.signals.has_meaningful_images,
            "has_ocr_or_scanned_pages": a.signals.has_ocr_or_scanned_pages,
            "has_low_confidence_blocks": a.signals.has_low_confidence_blocks,
            "likely_graph_candidate": a.signals.likely_graph_candidate,
            "likely_requirement_document": a.signals.likely_requirement_document,
            "likely_financial_document": a.signals.likely_financial_document,
            "likely_technical_document": a.signals.likely_technical_document,
        },
        "recommended_steps": {
            "chunking": {
                "enabled": a.execution_plan.chunking.enabled,
                "strategy": a.execution_plan.chunking.strategy,
                "reason": a.execution_plan.chunking.reason,
            },
            **{
                step.step: {
                    "enabled": step.enabled,
                    "scope": step.scope,
                    "pages": list(step.pages),
                    "reason": step.reason,
                }
                for step in a.execution_plan.steps
            },
        },
        "warnings": list(a.warnings),
    }
