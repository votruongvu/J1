"""PlanningResult — persistent post-compile planning output.

Single source of truth for the on-disk shape of `planning_result.json`.
The activity layer writes one of these per document; the
`/ingestion-runs/{id}/planning` endpoint reads them back and serves
them as a `PlanningResultDTO` projection.

Two responsibilities live here:

1. **Schema + (de)serialisation.** A frozen dataclass that round-
   trips through JSON without losing fields. Backwards-compatible —
   unknown fields are tolerated so older runs keep loading after
   producers add new keys.

2. **Validation.** `validate_planning_result_dict()` checks a parsed
   JSON object (typically: an LLM response) against the wire shape
   *before* the workflow trusts it. Catches the LLM cases the
   spec calls out: invalid profile/type strings, missing reasons on
   enabled/disabled steps, out-of-range confidence, page numbers
   outside the document, full-content leaks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from j1.processing.document_understanding import (
    DOCUMENT_TYPES,
    DocumentType,
)
from j1.processing.post_compile_assessment import (
    ALL_STEP_NAMES,
    ALLOWED_CHUNK_STRATEGIES,
    ALLOWED_PROFILES,
    ALLOWED_SCOPES,
    PROFILE_BALANCED,
    PostCompileAssessment,
)


__all__ = [
    "PLANNING_RESULT_SCHEMA_VERSION",
    "PLANNING_SOURCE_LLM",
    "PLANNING_SOURCE_RULE_BASED",
    "PLANNING_SOURCE_RULE_BASED_FALLBACK",
    "PlanningResult",
    "PlanningValidationError",
    "assessment_to_planning_result",
    "validate_planning_result_dict",
]


PLANNING_RESULT_SCHEMA_VERSION = "1.0"

PLANNING_SOURCE_RULE_BASED = "rule_based"
PLANNING_SOURCE_LLM = "llm"
PLANNING_SOURCE_RULE_BASED_FALLBACK = "rule_based_fallback"


_ALLOWED_SOURCES: frozenset[str] = frozenset({
    PLANNING_SOURCE_RULE_BASED,
    PLANNING_SOURCE_LLM,
    PLANNING_SOURCE_RULE_BASED_FALLBACK,
})


# Rough cap on raw-content leaks — the spec's "no full document
# content" rule. Any single string in the payload longer than this is
# rejected during validation. The cap is generous enough that
# legitimate operator-readable summaries stay valid; a real document
# block will blow past it.
_MAX_PAYLOAD_STRING_CHARS = 4_000


class PlanningValidationError(ValueError):
    """Raised when a planning-result payload fails validation.

    The workflow catches this, logs the reason, and either falls
    back to rule-based plan (`fail_open=True`) or fails the planning
    step (`fail_open=False`)."""


# ---- Persistent shape -------------------------------------------------


@dataclass(frozen=True)
class PlanningResult:
    """The persistent shape — what the artifact stores.

    Round-trips through `to_dict()` / `from_dict()`. Keys mirror the
    wire schema verbatim (snake_case) so producers and consumers
    don't have to translate.

    `assessment` carries the rule-based output verbatim (so the FE
    Planning Report can compare LLM vs. rule decisions), while the
    top-level `recommended_profile` / `execution_plan` reflect the
    final winning decision (LLM if accepted, rule-based otherwise).

    `domain_context` is always populated — at minimum with the
    generic-fallback shape so consumers don't have to special-case
    its absence. The pack id + selection source explain how the
    plan got produced.
    """

    run_id: str
    document_id: str
    planning_version: str
    planning_phase: str
    source: str
    created_at: str
    recommended_profile: str
    confidence: float
    document_understanding: dict[str, Any]
    decision_summary: dict[str, Any]
    content_report: dict[str, Any]
    quality_report: dict[str, Any]
    execution_plan: dict[str, Any]
    rule_based_assessment: dict[str, Any]
    rule_based_comparison: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    domain_context: dict[str, Any] = field(default_factory=dict)
    # Operator-facing planner mode. Mirrors the spec's
    # `plannerMode` wire field. One of:
    #   * `rule_based`           — deterministic only
    #   * `llm`                  — LLM ran and its output was accepted
    #   * `hybrid`               — both ran; rule-based + LLM merge
    #   * `rule_based_fallback`  — LLM ran but failed/invalid, kept
    #                              rule-based output
    planner_mode: str = "rule_based"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_bytes(self) -> bytes:
        """Encode to UTF-8 JSON for storage as artifact content."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanningResult":
        """Reconstruct from a stored JSON document. Tolerant to
        missing keys — older runs that pre-date a field default to
        empty values rather than raise."""
        return cls(
            run_id=str(data.get("run_id", "")),
            document_id=str(data.get("document_id", "")),
            planning_version=str(
                data.get("planning_version", PLANNING_RESULT_SCHEMA_VERSION),
            ),
            planning_phase=str(data.get("planning_phase", "post_compile")),
            source=str(data.get("source", PLANNING_SOURCE_RULE_BASED)),
            created_at=str(data.get("created_at", "")),
            recommended_profile=str(
                data.get("recommended_profile", PROFILE_BALANCED),
            ),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            document_understanding=dict(
                data.get("document_understanding") or {},
            ),
            decision_summary=dict(data.get("decision_summary") or {}),
            content_report=dict(data.get("content_report") or {}),
            quality_report=dict(data.get("quality_report") or {}),
            execution_plan=dict(data.get("execution_plan") or {}),
            rule_based_assessment=dict(
                data.get("rule_based_assessment") or {},
            ),
            rule_based_comparison=dict(
                data.get("rule_based_comparison") or {},
            ),
            warnings=[
                str(w) for w in (data.get("warnings") or [])
                if isinstance(w, str)
            ],
            next_actions=[
                str(a) for a in (data.get("next_actions") or [])
                if isinstance(a, str)
            ],
            domain_context=dict(data.get("domain_context") or {}),
            planner_mode=str(
                data.get("planner_mode")
                # Backward compat with older artifacts that only set
                # `source`. Project the legacy value when planner_mode
                # is absent: `llm` → llm, `rule_based_fallback` →
                # rule_based_fallback, `rule_based` → rule_based.
                or data.get("source")
                or "rule_based"
            ),
        )


# ---- Builder from rule-based assessment ------------------------------


def assessment_to_planning_result(
    *,
    run_id: str,
    document_id: str,
    created_at: str,
    assessment: PostCompileAssessment,
    source: str = PLANNING_SOURCE_RULE_BASED,
    rule_based_comparison: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> PlanningResult:
    """Project the rule-based `PostCompileAssessment` into the
    persistent `PlanningResult` shape.

    Used both as the rule-based path's final output and as the
    fall-back payload when the LLM-assisted planner fails."""
    rule_based_dict = _assessment_to_dict(assessment)
    if source == PLANNING_SOURCE_LLM:
        # The LLM path replaces this from its own validated dict;
        # this builder is the rule-based / fallback path only.
        raise ValueError(
            "assessment_to_planning_result is for rule-based / fallback "
            "outputs; LLM outputs go through validate_planning_result_dict."
        )
    return PlanningResult(
        run_id=run_id,
        document_id=document_id,
        planning_version=PLANNING_RESULT_SCHEMA_VERSION,
        planning_phase="post_compile",
        source=source,
        created_at=created_at,
        recommended_profile=assessment.recommended_profile,
        confidence=assessment.confidence,
        document_understanding=_understanding_to_dict(
            assessment.document_understanding,
        ),
        decision_summary={
            "overall_assessment": assessment.overall_assessment,
            "document_complexity": _complexity_for_signals(assessment),
            "parse_quality": assessment.quality_report.parse_confidence,
            "recommended_strategy": assessment.execution_plan.chunking.strategy,
            "main_reasoning": list(assessment.decision_summary_main_reasoning),
        },
        content_report={
            "language": assessment.content_report.language,
            "page_count": assessment.content_report.page_count,
            "structure_quality": assessment.content_report.structure_quality,
            "layout_complexity": assessment.content_report.layout_complexity,
            "content_density": assessment.content_report.content_density,
            "has_clear_sections": assessment.content_report.has_clear_sections,
            "has_tables": assessment.content_report.has_tables,
            "has_images": assessment.content_report.has_images,
            "has_formulas": assessment.content_report.has_formulas,
            "has_ocr_pages": assessment.content_report.has_ocr_pages,
            "important_observations": list(
                assessment.content_report.important_observations,
            ),
        },
        quality_report=_quality_report_to_dict(assessment.quality_report),
        execution_plan=_execution_plan_to_dict(assessment.execution_plan),
        rule_based_assessment=rule_based_dict,
        rule_based_comparison=rule_based_comparison or {},
        warnings=list(assessment.warnings),
        next_actions=next_actions or _default_next_actions(assessment),
    )


# ---- Validation -------------------------------------------------------


def validate_planning_result_dict(
    data: dict[str, Any],
    *,
    page_count: int | None = None,
    extended_document_types: frozenset[str] | None = None,
) -> None:
    """Validate a parsed planning-result payload.

    Raises `PlanningValidationError` on any violation. Used by the
    LLM-assist activity to gate untrusted model output before
    persisting; rule-based outputs go through `assessment_to_planning_result`
    directly and don't need this check.

    `page_count` (when provided) is the run's known page count —
    enabled per-page lists must be subsets of `range(1, page_count+1)`.
    Pass None when the page count is unknown; the page-bound check
    is skipped in that case.

    `extended_document_types` widens the allowed taxonomy with any
    types contributed by registered domain packs. Pass the union
    from `DomainRegistry.extended_document_types()` so a domain-
    pack-specific document_type validates without mutating the core
    enum. Defaults to None → only generic types are accepted.
    """
    if not isinstance(data, dict):
        raise PlanningValidationError("planning result must be a JSON object")

    profile = data.get("recommended_profile")
    if profile not in ALLOWED_PROFILES:
        raise PlanningValidationError(
            f"recommended_profile {profile!r} not in {sorted(ALLOWED_PROFILES)}"
        )

    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise PlanningValidationError("confidence must be a number")
    if not (0.0 <= float(confidence) <= 1.0):
        raise PlanningValidationError(
            f"confidence {confidence} not in [0.0, 1.0]"
        )

    allowed_types: frozenset[str] = (
        DOCUMENT_TYPES | (extended_document_types or frozenset())
    )
    understanding = data.get("document_understanding") or {}
    doc_type = understanding.get("document_type")
    if doc_type not in allowed_types:
        raise PlanningValidationError(
            f"document_understanding.document_type {doc_type!r} "
            f"not in taxonomy (registered types: {len(allowed_types)})"
        )

    plan = data.get("execution_plan") or {}
    if not isinstance(plan, dict):
        raise PlanningValidationError("execution_plan must be an object")

    steps = (plan.get("steps") or {}) if isinstance(plan.get("steps"), dict) else {}

    # Chunking lives inside `steps` per the canonical layout, but
    # legacy / convenience producers may put it at the top level —
    # accept either.
    chunking = (
        steps.get("chunking")
        or plan.get("chunking")
        or {}
    )
    if chunking:
        strategy = chunking.get("strategy")
        if strategy not in ALLOWED_CHUNK_STRATEGIES:
            raise PlanningValidationError(
                f"chunking.strategy {strategy!r} not in {sorted(ALLOWED_CHUNK_STRATEGIES)}"
            )
        if not chunking.get("reason"):
            raise PlanningValidationError("chunking entry must carry a reason")
    for step_name in ALL_STEP_NAMES:
        if step_name == "chunking":
            continue
        entry = steps.get(step_name)
        if entry is None:
            # Missing step entries are tolerated — caller may have
            # produced a partial plan. We coerce later.
            continue
        if not isinstance(entry, dict):
            raise PlanningValidationError(
                f"execution_plan.steps.{step_name} must be an object"
            )
        if "enabled" not in entry:
            raise PlanningValidationError(
                f"execution_plan.steps.{step_name} missing 'enabled' flag"
            )
        if not entry.get("reason"):
            raise PlanningValidationError(
                f"execution_plan.steps.{step_name} must carry a reason"
            )
        scope = entry.get("scope")
        if scope is not None and scope not in ALLOWED_SCOPES:
            raise PlanningValidationError(
                f"execution_plan.steps.{step_name}.scope {scope!r} "
                f"not in {sorted(ALLOWED_SCOPES)}"
            )
        # Page-list validation: must be ints, must be within document.
        pages = entry.get("pages") or []
        if pages and not isinstance(pages, list):
            raise PlanningValidationError(
                f"execution_plan.steps.{step_name}.pages must be a list"
            )
        for p in pages:
            if not isinstance(p, int):
                raise PlanningValidationError(
                    f"execution_plan.steps.{step_name}.pages must contain ints"
                )
            if p <= 0:
                raise PlanningValidationError(
                    f"execution_plan.steps.{step_name}.pages: {p} must be > 0"
                )
            if page_count is not None and p > page_count:
                raise PlanningValidationError(
                    f"execution_plan.steps.{step_name}.pages: {p} > page_count {page_count}"
                )
        # Graph step needs candidate_entity_types when enabled.
        if step_name == "graph_extraction" and entry.get("enabled"):
            cand = entry.get("candidate_entity_types") or []
            if not isinstance(cand, list):
                raise PlanningValidationError(
                    "graph_extraction.candidate_entity_types must be a list when enabled"
                )

    # Embedding + indexing should remain enabled unless a fatal parse
    # error is recorded — we don't have visibility into that here, so
    # we just warn-shape: missing 'enabled' is already caught above,
    # and a soft check follows.
    for required_step in ("embedding", "indexing"):
        entry = steps.get(required_step)
        if entry is None:
            continue
        if entry.get("enabled") is False:
            quality = (data.get("quality_report") or {}).get("parse_confidence")
            if quality != "low":
                raise PlanningValidationError(
                    f"{required_step} disabled but parse_confidence is not low"
                )

    # Raw-content leak guard.
    _check_no_raw_content(data, max_chars=_MAX_PAYLOAD_STRING_CHARS)


def _check_no_raw_content(value: object, *, max_chars: int) -> None:
    """Walk the value and raise when any string field exceeds the cap.

    Cheap defence against an LLM echoing the full document into the
    `summary` / `overall_assessment` / `text_preview` fields. The cap
    is generous (4000 chars) so legitimate operator-readable text
    stays valid; real document blocks will blow past it."""
    if isinstance(value, str):
        if len(value) > max_chars:
            raise PlanningValidationError(
                f"planning result contains a {len(value)}-char string "
                f"(cap {max_chars}); raw document content is forbidden"
            )
        return
    if isinstance(value, dict):
        for v in value.values():
            _check_no_raw_content(v, max_chars=max_chars)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _check_no_raw_content(v, max_chars=max_chars)
        return
    # int / float / bool / None → fine.


# ---- Internals --------------------------------------------------------


def _understanding_to_dict(u) -> dict[str, Any]:
    """Render a `DocumentUnderstanding` into the wire schema's
    `document_understanding` shape."""
    return {
        "title_source": u.title_source,
        "detected_title": u.detected_title,
        "title_quality": u.title_quality,
        "document_type": (
            u.document_type.value
            if isinstance(u.document_type, DocumentType)
            else str(u.document_type)
        ),
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
        "evidence": [
            {
                "source": e.source,
                "page": e.page,
                "text_preview": e.text_preview,
                "reason": e.reason,
            }
            for e in u.evidence
        ],
        "warnings": list(u.warnings),
    }


def _execution_plan_to_dict(plan) -> dict[str, Any]:
    chunking = plan.chunking
    steps_dict: dict[str, Any] = {
        "chunking": {
            "enabled": chunking.enabled,
            "strategy": chunking.strategy,
            "reason": chunking.reason,
            "settings": dict(chunking.settings),
        }
    }
    for step in plan.steps:
        entry: dict[str, Any] = {
            "enabled": step.enabled,
            "scope": step.scope,
            "pages": list(step.pages),
            "reason": step.reason,
        }
        if step.candidate_entity_types:
            entry["candidate_entity_types"] = list(step.candidate_entity_types)
        if step.model_profile:
            entry["model_profile"] = step.model_profile
        if step.settings:
            entry["settings"] = dict(step.settings)
        steps_dict[step.step] = entry
    return {
        "estimated_time": plan.estimated_time,
        "estimated_cost": plan.estimated_cost,
        "steps": steps_dict,
    }


def _quality_report_to_dict(q) -> dict[str, Any]:
    return {
        "parse_confidence": q.parse_confidence,
        "risk_level": q.risk_level,
        "detected_issues": [
            {
                "issue": iss.issue,
                "severity": iss.severity,
                "affected_pages": list(iss.affected_pages),
                "recommendation": iss.recommendation,
            }
            for iss in q.detected_issues
        ],
        "manual_review_required": q.manual_review_required,
        "manual_review_candidates": [
            {
                "page": c.page,
                "reason": c.reason,
                "block_types": list(c.block_types),
            }
            for c in q.manual_review_candidates
        ],
    }


def _assessment_to_dict(a: PostCompileAssessment) -> dict[str, Any]:
    """Render the full rule-based assessment as a dict for the
    `rule_based_assessment` slot in the artifact."""
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
        "warnings": list(a.warnings),
    }


def _complexity_for_signals(a: PostCompileAssessment) -> str:
    high = (
        a.signals.has_meaningful_tables
        and a.signals.has_meaningful_images
        and a.content_report.layout_complexity == "high"
    )
    if high:
        return "high"
    if a.signals.has_meaningful_tables or a.signals.has_meaningful_images:
        return "medium"
    return "low"


def _default_next_actions(a: PostCompileAssessment) -> list[str]:
    actions: list[str] = []
    enabled_steps = [s.step for s in a.execution_plan.steps if s.enabled]
    if enabled_steps:
        actions.append(
            f"Execute the following steps next: {', '.join(enabled_steps)}."
        )
    if a.quality_report.manual_review_required:
        actions.append("Manual review is recommended before promoting this run.")
    if a.recommended_profile == "diagnostic":
        actions.append(
            "Diagnostic mode — review parse quality before relying on outputs."
        )
    return actions
