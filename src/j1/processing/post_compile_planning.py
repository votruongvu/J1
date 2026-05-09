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

from j1.processing.content_digest import ContentDigest, build_content_digest
from j1.processing.document_understanding import (
    DocumentMetadata,
    DocumentUnderstanding,
    assess_document_understanding,
)
from j1.processing.manifest import ParsedContentManifest
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
) -> PlanningResult:
    """Build the full `PlanningResult` from compile outputs.

    Pure function — caller injects the LLM planner; we don't bind a
    transport here. Returns a result whose `source` field is one of:
      * `rule_based` — LLM disabled or no planner provided.
      * `llm` — LLM ran and its output validated.
      * `rule_based_fallback` — LLM ran but failed validation /
        threw / produced unsafe output and `fail_open=True`.

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

    rule_based_result = assessment_to_planning_result(
        run_id=run_id,
        document_id=document.document_id,
        created_at=timestamp,
        assessment=rule_based,
        source=PLANNING_SOURCE_RULE_BASED,
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

    return _merge_llm_into_rule_based(
        rule_based_result=rule_based_result,
        llm_payload=llm_payload,
        timestamp=timestamp,
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
    )


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
