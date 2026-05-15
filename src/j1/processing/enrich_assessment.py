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

Deployment-wide auto-run gate: ``J1_DOMAIN_ENRICHMENT_AUTO_ENABLED``
(default ``false``) suppresses every "this run should auto-enrich"
verdict at the planner. When false, the verdict becomes ``SKIP``
with reason ``"auto_enrichment_disabled"`` regardless of compile
signals. A domain policy that explicitly says ``ENRICHMENT_POLICY_ALWAYS``
still wins (per-domain compliance opt-in), and the explicit Manual
Run Domain Enrichment surface is unaffected — it dispatches an
explicit operator action that bypasses this planner entirely.
"""

from __future__ import annotations

import os
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
from j1.processing.enrichment_policy import ResolvedEnrichmentPolicy


# Deployment-wide auto-enrichment gate. Default ``false`` per
# product spec: the standard ingest path stays lightweight; richer
# behaviour is explicit / manual.
ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED = "J1_DOMAIN_ENRICHMENT_AUTO_ENABLED"
_AUTO_ENRICHMENT_DISABLED_REASON = (
    "auto_enrichment_disabled: J1_DOMAIN_ENRICHMENT_AUTO_ENABLED is "
    "off. Trigger Run Domain Enrichment manually if needed."
)


def _is_auto_enrichment_enabled(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = source.get(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED)
    if raw is None:
        return False  # default: auto is OFF
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


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
    #  closure: compile-stage warnings the parser surfaced
    # (e.g. "low-density page 3", "no language detected"). Used by
    # the assessor to bias OPTIONAL → RECOMMENDED when degraded
    # extraction is detected. Empty when the parser surfaced no
    # warnings or the workflow didn't thread them in.
    compile_warnings: tuple[str, ...] = ()
    #  closure: typed quality scores the parser surfaced
    # (0..1 each). The assessor consults these directly so the
    # low-quality bias doesn't depend on the discrete
    # `final_compile_quality` verdict alone.
    parse_quality_score: float | None = None
    text_sufficiency_score: float | None = None
    layout_complexity_score: float | None = None


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
    # ---- closure fields ---------------------------------
    # Operator-readable confidence (0..1) in the verdict. Heuristic:
    # 1.0 for blocking SKIPs, 0.85 when a policy=always / =never
    # drove the call, 0.7 when compile signals clearly support the
    # verdict, 0.5 for ambiguous OPTIONAL fallbacks.
    confidence: float = 0.5
    # Artifact kinds the recommended tasks are expected to produce.
    # Mirrors the FE's "what enrichment will add" tile copy. Derived
    # from `recommended_tasks` via `_TASK_TO_EXPECTED_OUTPUTS`.
    expected_outputs: tuple[str, ...] = ()
    # Whether the run should fail if enrichment fails. Sourced from
    # the active domain pack's policy
    # (`DomainEnrichmentPolicy.require_enrichment_success`).
    require_enrichment_success: bool = False
    # Preferred model tier (fast / premium / vision) for the
    # enrichment stage. Sourced from
    # `DomainEnrichmentPolicy.default_model_tier`; None inherits
    # the deployment default.
    model_tier_selection: str | None = None
    # Suggested concurrency knobs the workflow / enricher layer
    # may consult. Keys today: `default_model_tier` (mirrored from
    # `model_tier_selection`); future keys: `max_concurrent_llm_calls`,
    # `enrichment_timeout_seconds`. Empty = inherit deployment.
    concurrency_hints: dict[str, Any] = field(default_factory=dict)
    # Non-blocking caveats (low compile quality, missing language,
    # plan warnings from the parser). Distinct from `reasons` (why
    # the verdict is what it is) and `blocking_issues` (terminal SKIP
    # reasons). The FE renders these as a separate "heads up" tile.
    warnings: tuple[str, ...] = ()

    @property
    def should_enrich(self) -> bool:
        """Boolean projection of `overall_recommendation`.

 True for OPTIONAL / RECOMMENDED / REQUIRED — any case where
 the assessor didn't actively reject enrichment. False for
 SKIP. The boolean is what most downstream consumers branch
 on; the enum stays available for callers that need the
 finer-grained verdict."""
        return self.overall_recommendation != EnrichRecommendation.SKIP

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overall_recommendation": self.overall_recommendation.value,
            "should_enrich": self.should_enrich,
            "reasons": list(self.reasons),
            "recommended_tasks": list(self.recommended_tasks),
            "skipped_tasks": list(self.skipped_tasks),
            "blocking_issues": list(self.blocking_issues),
            "warnings": list(self.warnings),
            "source_signals": dict(self.source_signals),
            "decision_source": self.decision_source,
            "domain_id": self.domain_id,
            "domain_enrichment_policy": dict(self.domain_enrichment_policy),
            "confidence": self.confidence,
            "expected_outputs": list(self.expected_outputs),
            "require_enrichment_success": self.require_enrichment_success,
            "model_tier_selection": self.model_tier_selection,
            "concurrency_hints": dict(self.concurrency_hints),
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
            confidence=float(payload.get("confidence", 0.5) or 0.0),
            expected_outputs=tuple(payload.get("expected_outputs") or ()),
            require_enrichment_success=bool(
                payload.get("require_enrichment_success") or False
            ),
            model_tier_selection=(
                str(payload["model_tier_selection"])
                if payload.get("model_tier_selection") else None
            ),
            concurrency_hints=dict(payload.get("concurrency_hints") or {}),
            warnings=tuple(payload.get("warnings") or ()),
        )


# Task → expected artifact-output kinds. Mirrors the enricher
# classes' `artifact_type` constants in `j1.enrichers` so the FE
# renders accurate "Enrichment will add: …" tile copy without
# branching on task ids. Empty list means the task produces no
# named artifact (e.g. quality_assessment writes to metadata only).
_TASK_TO_EXPECTED_OUTPUTS: dict[str, tuple[str, ...]] = {
    TASK_TABLE_ENRICHMENT: ("enriched.tables",),
    TASK_IMAGE_CAPTIONING: ("enriched.visuals",),
    TASK_VISION_ENRICHMENT: ("enriched.visuals",),
    TASK_REQUIREMENT_EXTRACTION: ("enriched.requirements",),
    TASK_RISK_EXTRACTION: ("enriched.risks",),
    TASK_QUALITY_ASSESSMENT: ("enriched.confidence_assessment",),
}


def _expected_outputs_for_tasks(
    tasks: tuple[str, ...],
) -> tuple[str, ...]:
    """Project the recommended-task list onto the artifact kinds
 those tasks will produce. Deduplicates while preserving order
 so a (table_enrichment, image_captioning, vision_enrichment)
 tuple yields ("enriched.tables", "enriched.visuals")."""
    seen: set[str] = set()
    out: list[str] = []
    for task in tasks:
        for output in _TASK_TO_EXPECTED_OUTPUTS.get(task, ()):
            if output in seen:
                continue
            seen.add(output)
            out.append(output)
    return tuple(out)


def _build_warnings(signals: SourceSignals) -> tuple[str, ...]:
    """Compose a non-blocking warning list from the compile signals.

 Mirrors the spec's "caveat surface" — distinct from `reasons`
 (verdict explanation) and `blocking_issues` (terminal SKIP)."""
    warnings: list[str] = []
    if signals.compile_warnings:
        warnings.extend(signals.compile_warnings)
    if signals.final_compile_quality == "low":
        warnings.append(
            "final compile quality is LOW; enrichment will run on "
            "degraded input"
        )
    if (
        signals.parse_quality_score is not None
        and signals.parse_quality_score < 0.5
    ):
        warnings.append(
            f"parser quality score {signals.parse_quality_score:.2f} "
            "is below 0.5; results may need review"
        )
    if signals.has_scanned_pages:
        warnings.append(
            "compile saw scanned pages; vision enrichment quality "
            "depends on the OCR mode used"
        )
    return tuple(warnings)


def _derive_confidence(
    *,
    recommendation: EnrichRecommendation,
    has_strong_signals: bool,
    policy_drove_decision: bool,
) -> float:
    """Operator-readable verdict confidence.

 Heuristic — not a probabilistic score. Used by the FE to render
 a confidence pill alongside the recommendation; not consumed by
 other decision logic."""
    if recommendation == EnrichRecommendation.SKIP:
        # Blocking SKIPs are deterministic, hence high confidence.
        return 1.0
    if policy_drove_decision:
        # Domain policy overrides have stable confidence — the
        # operator opted into them deliberately.
        return 0.85
    if recommendation in (
        EnrichRecommendation.RECOMMENDED, EnrichRecommendation.REQUIRED,
    ) and has_strong_signals:
        return 0.75
    return 0.5


def assess_post_compile_enrich(
    signals: SourceSignals,
    *,
    domain_pack: DomainPack | None = None,
    initial_plan_candidates: tuple[str, ...] = (),
    resolved_policy: "ResolvedEnrichmentPolicy | None" = None,
) -> PostCompileEnrichPlan:
    """Rule-based assessor. Pure function — no I/O, no LLM.

 When `domain_pack` is provided AND carries a non-default
 `enrichment_policy`, the verdict + per-task lists are adjusted
 via `_apply_domain_policy` AFTER the rule-based decision. Blocking
 conditions (compile failure, empty document) remain authoritative —
 a domain policy can't override SKIP for those.

 `initial_plan_candidates` ( → bridge): the
 `InitialExecutionPlan.candidate_enrichment_modules` list. Tasks
 appearing here that the rule-based path didn't already
 recommend are added to `recommended_tasks` as domain-suggested
 optional candidates. Each addition records its provenance via a
 `reasons` entry "candidate from initial execution plan: <task>".

 `resolved_policy`: the layered policy resolution
 (request > project > domain > system). When provided, overrides
 the domain pack's policy field. Lets per-run operator overrides
 drive the verdict without modifying the pack."""
    if signals.compile_status == "failed":
        block = "compile failed; nothing to enrich"
        return _finalize_plan(
            PostCompileEnrichPlan(
                overall_recommendation=EnrichRecommendation.SKIP,
                reasons=(block,),
                blocking_issues=(block,),
                source_signals=_signals_to_dict(signals),
                domain_id=_domain_id(domain_pack),
                domain_enrichment_policy=_domain_policy_dict(domain_pack),
                warnings=_build_warnings(signals),
            ),
            signals=signals, domain_pack=domain_pack,
        )
    if signals.final_compile_quality == "failed":
        block = (
            "final compile quality is FAILED; enrichment would "
            "amplify low-quality input"
        )
        return _finalize_plan(
            PostCompileEnrichPlan(
                overall_recommendation=EnrichRecommendation.SKIP,
                reasons=(block,),
                blocking_issues=(block,),
                source_signals=_signals_to_dict(signals),
                domain_id=_domain_id(domain_pack),
                domain_enrichment_policy=_domain_policy_dict(domain_pack),
                warnings=_build_warnings(signals),
            ),
            signals=signals, domain_pack=domain_pack,
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
        return _finalize_plan(
            PostCompileEnrichPlan(
                overall_recommendation=EnrichRecommendation.SKIP,
                reasons=(block,),
                blocking_issues=(block,),
                source_signals=_signals_to_dict(signals),
                domain_id=_domain_id(domain_pack),
                domain_enrichment_policy=_domain_policy_dict(domain_pack),
                warnings=_build_warnings(signals),
            ),
            signals=signals, domain_pack=domain_pack,
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

    # Quality + degraded-extraction signals ( closure).
    low_quality_signal = signals.final_compile_quality == "low"
    low_parse_score = (
        signals.parse_quality_score is not None
        and signals.parse_quality_score < 0.5
    )
    has_compile_warnings = bool(signals.compile_warnings)
    degraded_extraction = (
        low_quality_signal or low_parse_score or has_compile_warnings
    )
    if low_quality_signal:
        recommended.append(TASK_QUALITY_ASSESSMENT)
        reasons.append("compile quality is LOW; quality_assessment recommended")
    elif low_parse_score:
        recommended.append(TASK_QUALITY_ASSESSMENT)
        reasons.append(
            f"parser quality score "
            f"{signals.parse_quality_score:.2f} below 0.5; "
            "quality_assessment recommended"
        )
    else:
        skipped.append(TASK_QUALITY_ASSESSMENT)
    if has_compile_warnings:
        reasons.append(
            f"compile surfaced {len(signals.compile_warnings)} "
            "warning(s); enrichment may add useful retrieval metadata"
        )

    # Requirement / risk extraction are domain-opted-in only — the
    # rule-based assessor doesn't read content semantics. A domain
    # pack's `force_recommended_tasks` upgrades them below.
    skipped.append(TASK_REQUIREMENT_EXTRACTION)
    skipped.append(TASK_RISK_EXTRACTION)

    if recommended:
        overall = EnrichRecommendation.RECOMMENDED
    elif degraded_extraction:
        #  closure: degraded-extraction bias. Even when no
        # tables/images are present, low quality / compile warnings
        # justify lifting OPTIONAL → RECOMMENDED so the enrichment
        # stage can add retrieval hints / quality notes.
        overall = EnrichRecommendation.RECOMMENDED
        reasons.append(
            "degraded extraction signals detected; enrichment "
            "recommended to surface retrieval-friendly metadata"
        )
    else:
        overall = EnrichRecommendation.OPTIONAL
        reasons.append(
            "no rich content signals (images/tables); enrichment optional"
        )

    #  merge initial-plan candidates as optional
    # additions BEFORE the policy overlay runs. This makes the
    # initial-plan's suggestion explicit on the recommended list
    # so the FE can render "candidate from initial execution plan".
    # The domain policy can still demote them via `denied_tasks`.
    for candidate in initial_plan_candidates:
        if not candidate:
            continue
        if candidate in recommended:
            continue
        recommended.append(candidate)
        if candidate in skipped:
            skipped.remove(candidate)
        reasons.append(
            f"candidate from initial execution plan: {candidate}"
        )
    # Promotion: if any initial-plan candidates landed and the
    # rule-based verdict is OPTIONAL, lift to RECOMMENDED so the FE
    # banner reflects the candidate intent.
    if (
        initial_plan_candidates
        and recommended
        and overall == EnrichRecommendation.OPTIONAL
    ):
        overall = EnrichRecommendation.RECOMMENDED

    plan = PostCompileEnrichPlan(
        overall_recommendation=overall,
        reasons=tuple(reasons),
        recommended_tasks=tuple(recommended),
        skipped_tasks=tuple(skipped),
        source_signals=_signals_to_dict(signals),
        domain_id=_domain_id(domain_pack),
        domain_enrichment_policy=_domain_policy_dict(domain_pack),
        warnings=_build_warnings(signals),
    )
    effective_policy = _effective_policy_for_overlay(
        domain_pack=domain_pack, resolved_policy=resolved_policy,
    )
    if effective_policy is not None:
        plan = _apply_domain_policy(plan, effective_policy)
    # Deployment-wide auto-run gate. Default OFF — the standard
    # ingest path stays lightweight. A domain pack with an explicit
    # ``ALWAYS`` policy is honoured (per-domain compliance opt-in);
    # everything else downgrades to SKIP with an audit reason. The
    # explicit Manual Run Domain Enrichment surface dispatches its
    # own run and never reaches this planner, so the gate does not
    # affect operator-triggered enrichment.
    if not _is_auto_enrichment_enabled():
        policy_is_always = (
            effective_policy is not None
            and effective_policy.policy == ENRICHMENT_POLICY_ALWAYS
        )
        if not policy_is_always and plan.overall_recommendation in (
            EnrichRecommendation.RECOMMENDED,
            EnrichRecommendation.REQUIRED,
            EnrichRecommendation.OPTIONAL,
        ):
            from dataclasses import replace as _replace
            plan = _replace(
                plan,
                overall_recommendation=EnrichRecommendation.SKIP,
                reasons=plan.reasons + (_AUTO_ENRICHMENT_DISABLED_REASON,),
                blocking_issues=(
                    plan.blocking_issues
                    + (_AUTO_ENRICHMENT_DISABLED_REASON,)
                ),
            )
    return _finalize_plan(
        plan,
        signals=signals,
        domain_pack=domain_pack,
        resolved_policy=resolved_policy,
    )


def _effective_policy_for_overlay(
    *,
    domain_pack: DomainPack | None,
    resolved_policy: ResolvedEnrichmentPolicy | None,
) -> DomainEnrichmentPolicy | None:
    """Pick the policy snapshot used by `_apply_domain_policy`.

 `resolved_policy` carries the operator/project/system precedence
 chain. When present and its `policy` differs from the domain
 pack's, we synthesise a new `DomainEnrichmentPolicy` carrying
 the resolved policy literal but the domain pack's task lists +
 reasoning. This lets a per-run `never` override collapse the
 verdict to SKIP while the FE still sees the domain context."""
    if resolved_policy is None:
        return domain_pack.enrichment_policy if domain_pack else None
    base = (
        domain_pack.enrichment_policy
        if domain_pack else DomainEnrichmentPolicy()
    )
    if base.policy == resolved_policy.policy:
        return base
    # Synthesize an overlay carrying the resolved policy literal.
    from dataclasses import replace as _replace
    return _replace(base, policy=resolved_policy.policy)


def _domain_id(pack: DomainPack | None) -> str | None:
    return pack.id if pack is not None else None


def _domain_policy_dict(pack: DomainPack | None) -> dict[str, Any]:
    if pack is None:
        return {}
    return pack.enrichment_policy.to_dict()


def _finalize_plan(
    plan: PostCompileEnrichPlan,
    *,
    signals: SourceSignals,
    domain_pack: DomainPack | None,
    resolved_policy: ResolvedEnrichmentPolicy | None = None,
) -> PostCompileEnrichPlan:
    """Populate closure fields on a plan that's otherwise
 complete.

 Computes `expected_outputs`, `confidence`, `require_enrichment_success`,
 `model_tier_selection`, and `concurrency_hints` from the plan's
 recommended tasks + the active domain policy. Pure / no I/O —
 safe to call from rule-based + LLM-refined paths.

 When `resolved_policy` is provided, the plan's
 `domain_enrichment_policy` dict carries an extra `resolved`
 block recording the (policy, source) pair so the FE can render
 "Policy: never (from request)" alongside the verdict."""
    policy = (
        domain_pack.enrichment_policy if domain_pack is not None else None
    )
    expected_outputs = _expected_outputs_for_tasks(plan.recommended_tasks)
    require_success = policy.require_enrichment_success if policy else False
    model_tier = policy.default_model_tier if policy else None
    concurrency_hints: dict[str, Any] = {}
    if model_tier:
        concurrency_hints["default_model_tier"] = model_tier
    has_strong_signals = bool(
        signals.has_images
        or signals.image_count > 0
        or signals.has_tables
        or signals.table_count > 0
    )
    effective_policy_literal = (
        resolved_policy.policy
        if resolved_policy is not None
        else (policy.policy if policy else None)
    )
    policy_drove_decision = bool(
        effective_policy_literal
        in (ENRICHMENT_POLICY_ALWAYS, ENRICHMENT_POLICY_NEVER)
    )
    confidence = _derive_confidence(
        recommendation=plan.overall_recommendation,
        has_strong_signals=has_strong_signals,
        policy_drove_decision=policy_drove_decision,
    )
    # Surface the resolved policy on the plan's policy dict so the
    # FE / final report sees which precedence layer won.
    policy_dict = dict(plan.domain_enrichment_policy)
    if resolved_policy is not None:
        policy_dict["resolved"] = resolved_policy.to_dict()

    # Replace closure-fields only — preserve all other plan state.
    from dataclasses import replace as _replace
    return _replace(
        plan,
        expected_outputs=expected_outputs,
        require_enrichment_success=require_success,
        model_tier_selection=model_tier,
        concurrency_hints=concurrency_hints,
        confidence=confidence,
        domain_enrichment_policy=policy_dict,
    )


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
        #  closure fields — preserved through the overlay so a
        # downstream `_finalize_plan` call doesn't have to rederive
        # them. `_finalize_plan` runs AFTER this overlay and will
        # recompute expected_outputs / confidence anyway, but we
        # carry warnings here so the policy overlay never erases
        # the parser-side caveats.
        warnings=plan.warnings,
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
        from dataclasses import replace as _replace
        return _replace(plan, decision_source=DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM)
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
    # Preserve closure fields (warnings, domain_id, policy snapshot,
    # confidence, expected_outputs,...) so an LLM refinement doesn't
    # wipe the rule-based assessor's earlier work. The expected_outputs
    # tuple is recomputed below from the new recommended_tasks; other
    # closure fields carry over unchanged.
    new_expected = _expected_outputs_for_tasks(tuple(recommended))
    from dataclasses import replace as _replace
    return _replace(
        plan,
        overall_recommendation=new_rec,
        reasons=tuple(reasons),
        recommended_tasks=tuple(recommended),
        skipped_tasks=tuple(skipped),
        decision_source=DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM,
        expected_outputs=new_expected,
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

    plan_warnings = cm.get("plan_warnings") or ()
    if isinstance(plan_warnings, (list, tuple)):
        compile_warnings_tuple = tuple(
            str(w) for w in plan_warnings if w
        )
    else:
        compile_warnings_tuple = ()
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
        compile_warnings=compile_warnings_tuple,
        parse_quality_score=_opt_float(cs.get("parse_quality_score")),
        text_sufficiency_score=_opt_float(cs.get("text_sufficiency_score")),
        layout_complexity_score=_opt_float(cs.get("layout_complexity_score")),
    )


def build_signals_from_normalized_compile_result(
    normalized: Any,  # NormalizedCompileResult (lazy ref to avoid circular import)
) -> SourceSignals:
    """Project a `NormalizedCompileResult` onto the
 rule-based assessor's `SourceSignals` shape.

 Bridges → callers that have the typed
 `NormalizedCompileResult` (post-compile + summary persisted)
 can feed the assessor directly without re-deriving from the
 raw `ArtifactActivityResult` dicts. Pure / no I/O."""
    quality = normalized.quality_signals
    return SourceSignals(
        compile_status=normalized.status or "succeeded",
        final_compile_quality=normalized.final_quality_verdict or "good",
        page_count=normalized.page_count,
        text_extractable_ratio=quality.text_extractable_ratio,
        has_images=bool(normalized.detected_images)
            or "images" in normalized.detected_content_types,
        has_tables=bool(normalized.detected_tables)
            or "tables" in normalized.detected_content_types,
        has_scanned_pages=(
            "scanned_pages" in normalized.detected_content_types
        ),
        image_count=len(normalized.detected_images),
        table_count=len(normalized.detected_tables),
        text_block_count=normalized.text_block_count or 0,
        total_text_chars=normalized.extracted_text_chars,
        compile_warnings=normalized.warnings,
        parse_quality_score=quality.parse_quality_score,
        text_sufficiency_score=quality.text_sufficiency_score,
        layout_complexity_score=quality.layout_complexity_score,
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
