"""Post-compile enrich assessment.

Runs AFTER compile success. Inspects parsed-content signals
(image/table counts, final compile quality) and produces a
structured `PostCompileEnrichPlan` recommending whether to run
the LLM-cost enrichment stages downstream.

Rule-based today. The assessor is a pure function — easy to unit
test and safe inside a Temporal activity. A future "fast LLM"
mode (gated on `J1_ENRICH_FAST_LLM_ENABLED`) can sit in front of
the rule-based path for ambiguous cases without changing the
return shape; the schema is forward-compatible via
`decision_source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = "1"


class EnrichRecommendation(StrEnum):
    SKIP = "skip"
    OPTIONAL = "optional"
    RECOMMENDED = "recommended"
    REQUIRED = "required"


# Decision-source vocabulary written into the persisted plan so the
# FE can render "Decided by rule-based assessor" vs "Decided by
# fast LLM consultation". Keep stable — UI labels key off this.
DECISION_SOURCE_RULE_BASED = "rule_based"
DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM = "rule_based_with_fast_llm"


# Recognised enrich-task ids. Match the existing enricher kinds the
# workflow knows about (`enricher_kind` on the request) — adding a
# new task here without a corresponding enricher just means the
# recommendation is informational. Removing one risks the FE dropping
# a recommended task it can't render; bump the schema_version when
# changing this set.
TASK_TABLE_ENRICHMENT = "table_enrichment"
TASK_IMAGE_CAPTIONING = "image_captioning"
TASK_VISION_ENRICHMENT = "vision_enrichment"
TASK_REQUIREMENT_EXTRACTION = "requirement_extraction"
TASK_RISK_EXTRACTION = "risk_extraction"
TASK_QUALITY_ASSESSMENT = "quality_assessment"


@dataclass(frozen=True)
class SourceSignals:
    """Compact compile-time signals consumed by the assessor.

    Built by the activity from `ArtifactActivityResult.content_stats`
    + `ArtifactActivityResult.compile_metrics` + the
    `compile_strategy_report` final-quality verdict. Never carries
    document content — only counts and flags so the assessment is
    deterministic + easy to log."""
    compile_status: str  # "succeeded" / "failed"
    final_compile_quality: str = "good"  # good / low / failed
    page_count: int | None = None
    text_extractable_ratio: float | None = None
    has_images: bool = False
    has_tables: bool = False
    has_scanned_pages: bool = False
    image_count: int = 0
    table_count: int = 0
    text_block_count: int = 0
    total_text_chars: int = 0


@dataclass(frozen=True)
class PostCompileEnrichPlan:
    """Structured assessment surfaced as a `post_compile_enrich_plan`
    artifact + returned to the workflow for downstream gating.

    Fields:
      * `overall_recommendation` — high-level verdict the FE renders
        as a banner (SKIP / OPTIONAL / RECOMMENDED / REQUIRED).
      * `reasons` — operator-readable strings explaining the verdict.
      * `recommended_tasks` / `skipped_tasks` — explicit per-task
        decisions; intentionally surfaced even when a task is skipped
        so the FE can render "we considered X and decided no" vs
        silently omitting.
      * `blocking_issues` — populated only when the verdict is SKIP
        for a reason the operator should fix (e.g. compile failed).
      * `source_signals` — frozen snapshot of the inputs the assessor
        saw, for audit / debugging.
      * `decision_source` — `rule_based` today; future fast-LLM
        consults set `rule_based_with_fast_llm`."""

    overall_recommendation: EnrichRecommendation
    schema_version: str = SCHEMA_VERSION
    reasons: tuple[str, ...] = ()
    recommended_tasks: tuple[str, ...] = ()
    skipped_tasks: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    source_signals: dict[str, Any] = field(default_factory=dict)
    decision_source: str = DECISION_SOURCE_RULE_BASED

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overall_recommendation": self.overall_recommendation.value,
            "reasons": list(self.reasons),
            "recommended_tasks": list(self.recommended_tasks),
            "skipped_tasks": list(self.skipped_tasks),
            "blocking_issues": list(self.blocking_issues),
            "source_signals": dict(self.source_signals),
            "decision_source": self.decision_source,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PostCompileEnrichPlan":
        return cls(
            schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
            overall_recommendation=EnrichRecommendation(
                payload.get("overall_recommendation") or EnrichRecommendation.OPTIONAL.value
            ),
            reasons=tuple(payload.get("reasons") or ()),
            recommended_tasks=tuple(payload.get("recommended_tasks") or ()),
            skipped_tasks=tuple(payload.get("skipped_tasks") or ()),
            blocking_issues=tuple(payload.get("blocking_issues") or ()),
            source_signals=dict(payload.get("source_signals") or {}),
            decision_source=str(
                payload.get("decision_source") or DECISION_SOURCE_RULE_BASED
            ),
        )


def assess_post_compile_enrich(signals: SourceSignals) -> PostCompileEnrichPlan:
    """Rule-based assessor. Pure function — no I/O, no LLM."""
    if signals.compile_status == "failed":
        block = "compile failed; nothing to enrich"
        return PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.SKIP,
            reasons=(block,),
            blocking_issues=(block,),
            source_signals=_signals_to_dict(signals),
        )
    if signals.final_compile_quality == "failed":
        block = (
            "final compile quality is FAILED; enrichment would "
            "amplify low-quality input"
        )
        return PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.SKIP,
            reasons=(block,),
            blocking_issues=(block,),
            source_signals=_signals_to_dict(signals),
        )
    # Affirmative-empty rule: SKIP only when we have positive evidence
    # the document is empty (no chunks, no text chars, no rich-content
    # signals). Pure-zero defaults from a parser that didn't surface
    # metrics at all → fall through to OPTIONAL instead, since the
    # absence of signals isn't proof of an empty doc.
    if (
        signals.total_text_chars == 0
        and signals.text_block_count == 0
        and signals.image_count == 0
        and signals.table_count == 0
        and signals.page_count is not None
        and signals.page_count > 0
    ):
        block = "compile produced no content blocks despite a non-empty source"
        return PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.SKIP,
            reasons=(block,),
            blocking_issues=(block,),
            source_signals=_signals_to_dict(signals),
        )

    reasons: list[str] = []
    recommended: list[str] = []
    skipped: list[str] = []

    if signals.has_tables or signals.table_count > 0:
        recommended.append(TASK_TABLE_ENRICHMENT)
        reasons.append(
            f"document contains {max(signals.table_count, 1)} table(s)"
        )
    else:
        skipped.append(TASK_TABLE_ENRICHMENT)

    if signals.has_images or signals.image_count > 0:
        recommended.append(TASK_IMAGE_CAPTIONING)
        recommended.append(TASK_VISION_ENRICHMENT)
        reasons.append(
            f"document contains {max(signals.image_count, 1)} image(s)"
        )
    else:
        skipped.append(TASK_IMAGE_CAPTIONING)
        skipped.append(TASK_VISION_ENRICHMENT)

    if signals.final_compile_quality == "low":
        recommended.append(TASK_QUALITY_ASSESSMENT)
        reasons.append("compile quality is LOW; quality_assessment recommended")
    else:
        skipped.append(TASK_QUALITY_ASSESSMENT)

    # Requirement / risk extraction default skipped unless explicitly
    # opted into via domain hints (deferred — domain wiring lives on
    # the request, not on the assessor input).
    skipped.append(TASK_REQUIREMENT_EXTRACTION)
    skipped.append(TASK_RISK_EXTRACTION)

    if recommended:
        overall = EnrichRecommendation.RECOMMENDED
    else:
        overall = EnrichRecommendation.OPTIONAL
        reasons.append(
            "no rich content signals (images/tables); enrichment optional"
        )

    return PostCompileEnrichPlan(
        overall_recommendation=overall,
        reasons=tuple(reasons),
        recommended_tasks=tuple(recommended),
        skipped_tasks=tuple(skipped),
        source_signals=_signals_to_dict(signals),
    )


@dataclass(frozen=True)
class FastLLMRefinement:
    """A single refinement an optional fast-LLM consult emits to
    upgrade/downgrade a rule-based plan. Pure-data; the actual LLM
    call lives in a separate boundary (an activity / a wired
    consultant) so the rule-based assessor stays I/O-free.

    Honour-rules:
      * `recommendation` may upgrade OPTIONAL → RECOMMENDED/REQUIRED
        when the LLM justifies it, or downgrade RECOMMENDED →
        OPTIONAL when the LLM judges the doc isn't worth enriching.
        It must NEVER override SKIP — blocking conditions are
        deterministic.
      * `add_reasons` are appended to the existing reasons (capped
        to keep audit logs lean).
      * `add_recommended_tasks` extend the recommended list (no dups);
        anything new also drops out of `skipped_tasks`.
    """

    recommendation: EnrichRecommendation | None = None
    add_reasons: tuple[str, ...] = ()
    add_recommended_tasks: tuple[str, ...] = ()


_REFINABLE_FROM = frozenset({
    EnrichRecommendation.OPTIONAL,
    EnrichRecommendation.RECOMMENDED,
})
_REFINEMENT_REASON_CAP = 8


def apply_fast_llm_refinement(
    plan: PostCompileEnrichPlan,
    refinement: FastLLMRefinement,
) -> PostCompileEnrichPlan:
    """Merge a fast-LLM refinement with a rule-based plan and return a
    new `PostCompileEnrichPlan` with `decision_source` flipped to
    `rule_based_with_fast_llm`. SKIP plans are NEVER refined — the
    blocking conditions that drove SKIP are deterministic and must
    win over LLM judgement.

    Pure function — no I/O, no LLM call. Test fixtures construct
    a `FastLLMRefinement` directly; production wiring uses an
    activity that calls the LLM and constructs the same dataclass."""
    if plan.overall_recommendation == EnrichRecommendation.SKIP:
        # Refinement on a SKIP plan is rejected silently — the
        # rule-based blockers are authoritative. We still flip the
        # decision_source so audit logs record that an LLM consult
        # happened (and was overruled).
        return PostCompileEnrichPlan(
            schema_version=plan.schema_version,
            overall_recommendation=plan.overall_recommendation,
            reasons=plan.reasons,
            recommended_tasks=plan.recommended_tasks,
            skipped_tasks=plan.skipped_tasks,
            blocking_issues=plan.blocking_issues,
            source_signals=plan.source_signals,
            decision_source=DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM,
        )
    new_rec = plan.overall_recommendation
    if (
        refinement.recommendation is not None
        and plan.overall_recommendation in _REFINABLE_FROM
        and refinement.recommendation != EnrichRecommendation.SKIP
    ):
        new_rec = refinement.recommendation
    # Merge tasks: anything LLM recommended joins recommended_tasks
    # and is removed from skipped_tasks. Order is preserved
    # (rule-based first, then LLM additions in insertion order).
    recommended = list(plan.recommended_tasks)
    skipped = list(plan.skipped_tasks)
    for task in refinement.add_recommended_tasks:
        if task and task not in recommended:
            recommended.append(task)
        if task in skipped:
            skipped.remove(task)
    reasons = list(plan.reasons)
    for reason in refinement.add_reasons:
        if reason and reason not in reasons:
            reasons.append(reason)
    # Cap reasons so a chatty LLM doesn't bloat the artifact.
    reasons = reasons[:_REFINEMENT_REASON_CAP]
    return PostCompileEnrichPlan(
        schema_version=plan.schema_version,
        overall_recommendation=new_rec,
        reasons=tuple(reasons),
        recommended_tasks=tuple(recommended),
        skipped_tasks=tuple(skipped),
        blocking_issues=plan.blocking_issues,
        source_signals=plan.source_signals,
        decision_source=DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM,
    )


def build_signals_from_compile_metrics(
    *,
    compile_status: str,
    final_compile_quality: str,
    content_stats: dict[str, Any] | None,
    compile_metrics: dict[str, Any] | None,
) -> SourceSignals:
    """Construct `SourceSignals` from the workflow's
    `ArtifactActivityResult` shape. Defensive against missing keys —
    every field falls back to a safe default (zero counts / False
    flags / None for optionals)."""
    cs = content_stats or {}
    cm = compile_metrics or {}

    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _opt_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _opt_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return SourceSignals(
        compile_status=str(compile_status or "succeeded"),
        final_compile_quality=str(final_compile_quality or "good"),
        page_count=_opt_int(cs.get("page_count")),
        text_extractable_ratio=_opt_float(cs.get("text_extractable_ratio")),
        has_images=bool(cs.get("has_images") or cs.get("image_count")),
        has_tables=bool(cs.get("has_tables") or cs.get("table_count")),
        has_scanned_pages=bool(cs.get("has_scanned_pages")),
        image_count=_int(cs.get("image_count")),
        table_count=_int(cs.get("table_count")),
        text_block_count=_int(cs.get("text_block_count")),
        total_text_chars=_int(
            cs.get("total_text_chars") or cm.get("extracted_text_chars")
        ),
    )


def _signals_to_dict(s: SourceSignals) -> dict[str, Any]:
    return {
        "compile_status": s.compile_status,
        "final_compile_quality": s.final_compile_quality,
        "page_count": s.page_count,
        "text_extractable_ratio": s.text_extractable_ratio,
        "has_images": s.has_images,
        "has_tables": s.has_tables,
        "has_scanned_pages": s.has_scanned_pages,
        "image_count": s.image_count,
        "table_count": s.table_count,
        "text_block_count": s.text_block_count,
        "total_text_chars": s.total_text_chars,
    }
