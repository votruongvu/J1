"""Pre-compile initial execution plan.

`InitialExecutionPlan` is the cheap, deterministic, pre-compile
plan the workflow produces BEFORE dispatching the compile activity.
It carries:

 * the compile-engine intent (default RAGAnything),
 * the selected `domain_profile_id` and its `enrichment_policy`,
 * the candidate enrichment modules the domain pack suggests,
 * the cheap signals the planner inspected (extension, size,
 page count, basic text-extractability) so reviewers can audit
 the decision,
 * resource hints (concurrency / model tier suggestions) from the
 domain pack + deployment settings,
 * reasons + warnings the planner accumulated.

What the plan deliberately is NOT:
 * a final enrichment decision — that's `assess_post_compile_enrich`
 in `j1.processing.enrich_assessment`, which runs AFTER compile
 and sees the actual extraction signals.
 * a graph / index gate — graph and index activities are gated by
 request `enricher_kind` / `graph_builder_kind` / `indexer_kind`
 + post-compile signals, not by this plan.
 * a compile-config (mode / capabilities / parse_method) — those
 live on the wrapped `AssessmentPlan.compile_plan` and are
 consumed by the RAGAnything adapter.

The plan is PURE — no LLM, no OCR, no vision, no MinerU. Same
deterministic inputs across replay must produce the same plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from j1.domains.models import (
    ENRICHMENT_POLICY_AUTO,
    DomainPack,
)
from j1.processing.assessment import (
    AssessmentPlan,
    AssessmentPlanner,
    DefaultAssessmentPlanner,
)
from j1.processing.profiling import DocumentProfile


__all__ = [
    "COMPILE_ENGINE_RAGANYTHING",
    "InitialExecutionPlan",
    "build_initial_execution_plan",
]


# Wire vocabulary for `compile_engine`. RAGAnything is the only
# engine wired today; the field exists so a future alternative
# (raw-MinerU, a vendor-replacement adapter, etc.) is a one-line
# addition rather than a contract break.
COMPILE_ENGINE_RAGANYTHING = "raganything"


# Schema version for the persisted plan. Bump when adding a field
# whose absence changes consumer behaviour.
_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class InitialExecutionPlan:
    """The pre-compile initial execution plan for one document.

 See module docstring for the design contract. JSON-friendly via
 `to_payload` / `from_payload` so the Temporal data converter can
 transit the plan across activity boundaries without bespoke
 codecs."""

    document_id: str
    # Compile intent. Set to False only when a cheap signal proves
    # the document can't be compiled (e.g. empty file). The compile
    # stage is otherwise mandatory — every run that gets this far is
    # expected to compile via RAGAnything.
    run_compile: bool = True
    compile_engine: str = COMPILE_ENGINE_RAGANYTHING
    # Selected domain pack id. None when no pack was selected (the
    # registry isn't wired or domain packs are disabled). When set,
    # `enrichment_policy` and `candidate_enrichment_modules` are
    # derived from this pack's `DomainEnrichmentPolicy`.
    domain_profile_id: str | None = None
    # Enrichment policy literal — `auto` / `always` / `never`.
    # Mirrors the domain pack's policy when set; defaults to `auto`
    # when no pack is selected (legacy "let the assessor decide").
    enrichment_policy: str = ENRICHMENT_POLICY_AUTO
    # Modules the run is a candidate for. Sourced from
    # `policy.force_recommended_tasks ∪ policy.optional_tasks`. The
    # post-compile assessor refines this to `recommended_tasks`
    # using actual compile signals; the FE renders the candidate
    # list as "the domain would like to run these if signals match".
    candidate_enrichment_modules: tuple[str, ...] = ()
    # Snapshot of the cheap signals the planner inspected. Only
    # operational metadata — never document content. Keys mirror
    # `DocumentProfile` field names so reviewers can correlate.
    cheap_signals: dict[str, Any] = field(default_factory=dict)
    # Resource / concurrency hints surfaced for the workflow. Keys:
    # `default_model_tier` (from domain policy), `vlm_concurrency`
    # (from `J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY` — read at the
    # adapter, surfaced here for visibility), and future hints. The
    # plan only PROVIDES hints; it doesn't enforce them.
    resource_hints: dict[str, Any] = field(default_factory=dict)
    # Operator-readable explanation of why the plan looks like this.
    # Aggregates: domain selection trail, policy reasoning, cheap-
    # signal observations. The FE renders this as a bulleted "why"
    # list on the run-detail page.
    reasons: tuple[str, ...] = ()
    # Caveats: misconfiguration, low-confidence selection, hints
    # the planner couldn't apply. Non-blocking — runs still proceed
    # to compile when warnings exist.
    warnings: tuple[str, ...] = ()
    # The compile-stage knobs (mode / capabilities / parse method)
    # the RAGAnything adapter consumes. None when run_compile=False.
    # Carried inside the initial plan so callers have a single
    # structure to thread through; consumers that only want the
    # compile detail access `plan.compile_plan` directly.
    compile_plan: AssessmentPlan | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "document_id": self.document_id,
            "run_compile": self.run_compile,
            "compile_engine": self.compile_engine,
            "domain_profile_id": self.domain_profile_id,
            "enrichment_policy": self.enrichment_policy,
            "candidate_enrichment_modules": list(self.candidate_enrichment_modules),
            "cheap_signals": dict(self.cheap_signals),
            "resource_hints": dict(self.resource_hints),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "compile_plan": (
                self.compile_plan.to_payload() if self.compile_plan else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InitialExecutionPlan":
        compile_plan_payload = payload.get("compile_plan")
        compile_plan = (
            AssessmentPlan.from_payload(compile_plan_payload)
            if compile_plan_payload else None
        )
        return cls(
            document_id=str(payload.get("document_id") or ""),
            run_compile=bool(payload.get("run_compile", True)),
            compile_engine=str(
                payload.get("compile_engine") or COMPILE_ENGINE_RAGANYTHING
            ),
            domain_profile_id=(
                str(payload["domain_profile_id"])
                if payload.get("domain_profile_id") else None
            ),
            enrichment_policy=str(
                payload.get("enrichment_policy") or ENRICHMENT_POLICY_AUTO
            ),
            candidate_enrichment_modules=tuple(
                payload.get("candidate_enrichment_modules") or ()
            ),
            cheap_signals=dict(payload.get("cheap_signals") or {}),
            resource_hints=dict(payload.get("resource_hints") or {}),
            reasons=tuple(payload.get("reasons") or ()),
            warnings=tuple(payload.get("warnings") or ()),
            compile_plan=compile_plan,
        )


# ---- Builder --------------------------------------------------------


def build_initial_execution_plan(
    profile: DocumentProfile,
    *,
    domain_pack: DomainPack | None = None,
    compile_engine: str = COMPILE_ENGINE_RAGANYTHING,
    planner: AssessmentPlanner | None = None,
    document_type: str | None = None,
    resource_hints: dict[str, Any] | None = None,
) -> InitialExecutionPlan:
    """Build an `InitialExecutionPlan` from cheap inputs only.

 The builder reads:
 * `profile` — the cheap deterministic `DocumentProfile`
 (extension, size, page count, basic text-extractability).
 Must NOT carry LLM-derived signals.
 * `domain_pack` — the resolved pack (registry's `select_domain`
 output). None when no domain is active; the plan still
 works with `enrichment_policy=auto` and no candidates.
 * `compile_engine` — defaults to RAGAnything; surfaced so a
 future engine swap is a one-line addition.
 * `planner` — defaults to `DefaultAssessmentPlanner` for the
 compile-stage detail. Pass a stub in unit tests.
 * `document_type` — optional hint that the wrapped planner
 respects (filename-based pre-classification).
 * `resource_hints` — additional deployment-level hints
 (e.g. `vlm_concurrency`) the caller has already resolved.

 The builder is PURE: no I/O, no LLM. Same inputs → same plan.
 """
    chosen_planner = planner or DefaultAssessmentPlanner()
    compile_plan = chosen_planner.assess(profile, document_type=document_type)

    policy = (
        domain_pack.enrichment_policy
        if domain_pack is not None
        else None
    )
    enrichment_policy = (
        policy.policy if policy is not None else ENRICHMENT_POLICY_AUTO
    )
    candidates = _candidate_modules(policy)

    cheap_signals = _cheap_signals_from_profile(profile)
    hints = _build_resource_hints(policy, resource_hints)
    reasons, warnings = _build_reasons_and_warnings(
        profile, domain_pack, compile_plan,
    )

    return InitialExecutionPlan(
        document_id=profile.document_id,
        run_compile=True,
        compile_engine=compile_engine,
        domain_profile_id=domain_pack.id if domain_pack is not None else None,
        enrichment_policy=enrichment_policy,
        candidate_enrichment_modules=candidates,
        cheap_signals=cheap_signals,
        resource_hints=hints,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        compile_plan=compile_plan,
    )


def _candidate_modules(policy) -> tuple[str, ...]:
    """Union of force-recommended and optional task ids. Order
 preserved (force first, then optional) so the FE renders them
 in a sensible priority. Empty when no policy is supplied."""
    if policy is None:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for task in (*policy.force_recommended_tasks, *policy.optional_tasks):
        if task in seen:
            continue
        seen.add(task)
        out.append(task)
    return tuple(out)


def _cheap_signals_from_profile(
    profile: DocumentProfile,
) -> dict[str, Any]:
    """Snapshot the deterministic profile fields the plan exposes.

 Whitelisted to operational metadata only — no document content,
 no parser-quality scores that vary on the same input (those are
 parser-call derived, which means the profiler may run them but
 they're inherently parser-side). Keys mirror DocumentProfile."""
    return {
        "extension": profile.extension,
        "mime_type": profile.mime_type,
        "file_size_bytes": profile.file_size_bytes,
        "page_count": profile.page_count,
        "text_extractable_ratio": profile.text_extractable_ratio,
        "has_images": profile.has_images,
        "has_tables": profile.has_tables,
        "has_scanned_pages": profile.has_scanned_pages,
        "language": profile.language,
        "total_text_chars": profile.total_text_chars,
        "empty_page_ratio": profile.empty_page_ratio,
    }


def _build_resource_hints(
    policy,
    caller_hints: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge domain-policy hints with caller-provided hints.

 Caller hints win — they're typically the deployment's already-
 resolved values (e.g. `vlm_concurrency` from settings). Policy
 fills the gaps."""
    hints: dict[str, Any] = {}
    if policy is not None and policy.default_model_tier:
        hints["default_model_tier"] = policy.default_model_tier
    if caller_hints:
        for key, value in caller_hints.items():
            if value is None:
                continue
            hints[key] = value
    return hints


def _build_reasons_and_warnings(
    profile: DocumentProfile,
    domain_pack: DomainPack | None,
    compile_plan: AssessmentPlan,
) -> tuple[list[str], list[str]]:
    """Render operator-readable rationale for the plan.

 Reasons trail: domain selection (when set) → policy reasoning →
 compile-mode rationale. Warnings: low-confidence domain pick,
 profile signals that hint at parse difficulty."""
    reasons: list[str] = []
    warnings: list[str] = []

    if domain_pack is not None:
        reasons.append(
            f"domain pack: {domain_pack.id} (v{domain_pack.version})"
        )
        if domain_pack.enrichment_policy.reasoning:
            reasons.append(
                f"domain policy reasoning: "
                f"{domain_pack.enrichment_policy.reasoning}"
            )
        if domain_pack.enrichment_policy.policy != ENRICHMENT_POLICY_AUTO:
            reasons.append(
                f"enrichment policy: {domain_pack.enrichment_policy.policy}"
            )

    if compile_plan.reason:
        reasons.append(f"compile plan: {compile_plan.reason}")

    # Profile-derived caveats — never block, just surface so the FE
    # can render a "heads up" badge. Other warning conditions can
    # land here as the planner matures.
    if profile.warnings:
        warnings.extend(profile.warnings)
    if profile.has_scanned_pages is True:
        warnings.append(
            "profile indicates scanned pages; compile mode may "
            "escalate to deep"
        )

    return reasons, warnings
