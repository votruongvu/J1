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

Domain-aware overlay: when the run's selected `DomainPack` carries
a `DomainEnrichmentPolicy`, the assessor consults it for `always` /
`never` verdict adjustments and merges `force_recommended_tasks` /
`denied_tasks` into the per-task list. The pure-rule path remains
the default — generic / no-domain runs see no change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from j1.domains.models import (
    ENRICHMENT_POLICY_ALWAYS,
    ENRICHMENT_POLICY_AUTO,
    ENRICHMENT_POLICY_NEVER,
    DomainEnrichmentPolicy,
    DomainPack,
)


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
    # Domain pack id whose `DomainEnrichmentPolicy` shaped this plan.
    # None / "general" when the run had no active domain pack. The
    # FE renders "Domain policy: always (civil_engineering)" when set
    # so the operator sees the influence trail.
    domain_id: str | None = None
    # Snapshot of the applied policy. Empty dict when no policy was
    # consulted. Serialised as-is into the persisted plan.
    domain_enrichment_policy: dict[str, Any] = field(default_factory=dict)

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
            "domain_id": self.domain_id,
            "domain_enrichment_policy": dict(self.domain_enrichment_policy),
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
            domain_id=(
                str(payload["domain_id"])
                if payload.get("domain_id") else None
            ),
            domain_enrichment_policy=dict(
                payload.get("domain_enrichment_policy") or {}
            ),
        )


def assess_post_compile_enrich(
    signals: SourceSignals,
    *,
    domain_pack: DomainPack | None = None,
) -> PostCompileEnrichPlan:
    """Rule-based assessor. Pure function — no I/O, no LLM.

    When `domain_pack` is provided AND carries a non-default
    `enrichment_policy`, the verdict + per-task lists are adjusted
    via `_apply_domain_policy` AFTER the rule-based decision. Blocking
    conditions (compile failure, empty document) remain authoritative —
    a domain policy can't override SKIP for those."""
    if signals.compile_status == "failed":
        block = "compile failed; nothing to enrich"
        return PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.SKIP,
            reasons=(block,),
            blocking_issues=(block,),
            source_signals=_signals_to_dict(signals),
            domain_id=_domain_id(domain_pack),
            domain_enrichment_policy=_domain_policy_dict(domain_pack),
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
            domain_id=_domain_id(domain_pack),
            domain_enrichment_policy=_domain_policy_dict(domain_pack),
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
            domain_id=_domain_id(domain_pack),
            domain_enrichment_policy=_domain_policy_dict(domain_pack),
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

    # Requirement / risk extraction are domain-opted-in only — the
    # rule-based assessor doesn't read content semantics. A domain
    # pack's `force_recommended_tasks` upgrades them below.
    skipped.append(TASK_REQUIREMENT_EXTRACTION)
    skipped.append(TASK_RISK_EXTRACTION)

    if recommended:
        overall = EnrichRecommendation.RECOMMENDED
    else:
        overall = EnrichRecommendation.OPTIONAL
        reasons.append(
            "no rich content signals (images/tables); enrichment optional"
        )

    plan = PostCompileEnrichPlan(
        overall_recommendation=overall,
        reasons=tuple(reasons),
        recommended_tasks=tuple(recommended),
        skipped_tasks=tuple(skipped),
        source_signals=_signals_to_dict(signals),
        domain_id=_domain_id(domain_pack),
        domain_enrichment_policy=_domain_policy_dict(domain_pack),
    )
    if domain_pack is not None:
        plan = _apply_domain_policy(plan, domain_pack.enrichment_policy)
    return plan


def _domain_id(pack: DomainPack | None) -> str | None:
    return pack.id if pack is not None else None


def _domain_policy_dict(pack: DomainPack | None) -> dict[str, Any]:
    if pack is None:
        return {}
    return pack.enrichment_policy.to_dict()


def _apply_domain_policy(
    plan: PostCompileEnrichPlan,
    policy: DomainEnrichmentPolicy,
) -> PostCompileEnrichPlan:
    """Overlay the domain enrichment policy onto a rule-based plan.

    Rules:
      * `policy=never` → collapse to SKIP unless already blocked.
      * `policy=always` → upgrade OPTIONAL → RECOMMENDED.
      * Force-recommended tasks are added (deduped) and removed from
        `skipped_tasks`.
      * Denied tasks are removed from `recommended_tasks` and added
        to `skipped_tasks`.
      * `policy.reasoning` is appended to `reasons` so the operator
        sees the domain influence trail."""
    # SKIP from a blocking condition wins regardless of policy. The
    # rule-based path sets `blocking_issues` on those skips; we only
    # honour the policy on non-blocking SKIPs (which the current
    # rule-based path doesn't produce, but stays defensive).
    if plan.blocking_issues:
        return plan

    new_recommendation = plan.overall_recommendation
    new_reasons = list(plan.reasons)
    new_recommended = list(plan.recommended_tasks)
    new_skipped = list(plan.skipped_tasks)
    new_blocking = list(plan.blocking_issues)

    if policy.policy == ENRICHMENT_POLICY_NEVER:
        block = (
            f"domain policy=never (domain_id={plan.domain_id!r}) "
            "suppresses enrichment"
        )
        new_recommendation = EnrichRecommendation.SKIP
        new_reasons.append(block)
        new_blocking.append(block)
        # Move all recommended tasks to skipped so the FE renders the
        # full opt-out picture.
        new_skipped = list(set(new_skipped + new_recommended))
        new_recommended = []
    else:
        if policy.policy == ENRICHMENT_POLICY_ALWAYS:
            if new_recommendation in (
                EnrichRecommendation.OPTIONAL,
            ):
                new_recommendation = EnrichRecommendation.RECOMMENDED
                new_reasons.append(
                    f"domain policy=always (domain_id={plan.domain_id!r}) "
                    "upgraded recommendation"
                )

        # Force-recommended tasks: add (dedup) + drop from skipped.
        for task in policy.force_recommended_tasks:
            if task not in new_recommended:
                new_recommended.append(task)
                new_reasons.append(
                    f"domain force-recommended task: {task}"
                )
            if task in new_skipped:
                new_skipped.remove(task)

        # Denied tasks: drop from recommended + add to skipped.
        for task in policy.denied_tasks:
            if task in new_recommended:
                new_recommended.remove(task)
                new_reasons.append(
                    f"domain denied task: {task}"
                )
            if task not in new_skipped:
                new_skipped.append(task)

        # When force tasks lifted the recommended list out of empty,
        # upgrade OPTIONAL → RECOMMENDED so the UI banner reflects it.
        if (
            new_recommended
            and new_recommendation == EnrichRecommendation.OPTIONAL
        ):
            new_recommendation = EnrichRecommendation.RECOMMENDED

    if policy.reasoning:
        new_reasons.append(f"domain reasoning: {policy.reasoning}")

    return PostCompileEnrichPlan(
        overall_recommendation=new_recommendation,
        schema_version=plan.schema_version,
        reasons=tuple(new_reasons),
        recommended_tasks=tuple(new_recommended),
        skipped_tasks=tuple(new_skipped),
        blocking_issues=tuple(new_blocking),
        source_signals=plan.source_signals,
        decision_source=plan.decision_source,
        domain_id=plan.domain_id,
        domain_enrichment_policy=plan.domain_enrichment_policy,
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


@dataclass(frozen=True)
class FastLLMConsultPrompt:
    """Compact context the optional fast-LLM consult sees.

    Carries ONLY signals + the rule-based provisional decision. NEVER
    document content. Operators reading the consult's prompt logs see
    structured fields (counts, flags, recommendation). This bounds
    token cost and prevents accidental PII leakage."""

    compile_status: str
    final_compile_quality: str
    source_signals: dict[str, Any]
    provisional_recommendation: EnrichRecommendation
    provisional_recommended_tasks: tuple[str, ...]
    provisional_skipped_tasks: tuple[str, ...]
    compile_warnings: tuple[str, ...] = ()


# JSON schema the activity sends to the LLM via `extract` (or as
# explicit instructions when `generate` is used). Operators can
# inspect this directly when debugging fast-LLM responses; the parser
# below tolerates missing/extra fields so a chatty model doesn't
# break ingestion.
FAST_LLM_REFINEMENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["optional", "recommended", "required"],
            "description": (
                "Refined enrich recommendation. SKIP is intentionally "
                "not allowed — deterministic blocking conditions handle "
                "skip cases. Omit the field entirely when no refinement "
                "is warranted."
            ),
        },
        "add_reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short operator-readable justifications.",
        },
        "add_recommended_tasks": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    TASK_TABLE_ENRICHMENT,
                    TASK_IMAGE_CAPTIONING,
                    TASK_VISION_ENRICHMENT,
                    TASK_REQUIREMENT_EXTRACTION,
                    TASK_RISK_EXTRACTION,
                    TASK_QUALITY_ASSESSMENT,
                ],
            },
            "description": "Tasks the LLM judges should be added.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


def parse_fast_llm_refinement(
    payload: Any,
) -> FastLLMRefinement | None:
    """Parse a fast-LLM response (dict or JSON-string) into a
    `FastLLMRefinement`. Returns None when the payload is not a
    dict or has no usable fields. Hard rules:

      * `recommendation == "skip"` is silently dropped — SKIP is
        reserved for deterministic blocking conditions.
      * Unknown task ids in `add_recommended_tasks` are silently
        dropped (not raised) so a chatty model can't break ingestion.
      * Reasons are coerced to strings + capped downstream by
        `apply_fast_llm_refinement`.
    """
    import json as _json

    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None

    rec_value = payload.get("recommendation")
    rec: EnrichRecommendation | None = None
    if isinstance(rec_value, str):
        cleaned = rec_value.strip().lower()
        if cleaned in {"optional", "recommended", "required"}:
            rec = EnrichRecommendation(cleaned)
        # SKIP intentionally not honoured.

    raw_reasons = payload.get("add_reasons")
    reasons: tuple[str, ...] = ()
    if isinstance(raw_reasons, list):
        reasons = tuple(
            str(r) for r in raw_reasons
            if isinstance(r, (str, int, float)) and str(r).strip()
        )

    raw_tasks = payload.get("add_recommended_tasks")
    known_tasks = {
        TASK_TABLE_ENRICHMENT,
        TASK_IMAGE_CAPTIONING,
        TASK_VISION_ENRICHMENT,
        TASK_REQUIREMENT_EXTRACTION,
        TASK_RISK_EXTRACTION,
        TASK_QUALITY_ASSESSMENT,
    }
    tasks: tuple[str, ...] = ()
    if isinstance(raw_tasks, list):
        tasks = tuple(
            str(t).strip()
            for t in raw_tasks
            if isinstance(t, str) and str(t).strip() in known_tasks
        )

    if rec is None and not reasons and not tasks:
        return None
    return FastLLMRefinement(
        recommendation=rec,
        add_reasons=reasons,
        add_recommended_tasks=tasks,
    )


def is_consult_warranted(plan: PostCompileEnrichPlan) -> bool:
    """True iff the rule-based plan is ambiguous enough to be worth
    consulting a fast LLM. Today: only `OPTIONAL` plans qualify —
    SKIP is deterministic, RECOMMENDED/REQUIRED already carry
    confident rule-based reasons."""
    return plan.overall_recommendation == EnrichRecommendation.OPTIONAL


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
