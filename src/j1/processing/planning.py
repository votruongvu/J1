"""Adaptive ingestion planning contracts and modes.

A planner takes a `DocumentProfile` (and a policy + the current
`ProcessingCapabilities`) and emits an `IngestPlan` — a list of
`PlannedStep` records that the workflow uses to gate compile / enrich
/ graph / index.

Modes are pure data — frozen dataclasses mapping a logical name to a
set of enabled steps. Policies are the operator-controlled knob that
biases the planner toward "do less" or "do more". Both stay generic
and reusable: no domain-specific names, no phase labels, no client-
specific quirks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from j1.processing.profiling import DocumentProfile
from j1.processing.status import StepSource

__all__ = [
    "COST_TIER_HIGH",
    "COST_TIER_LOW",
    "COST_TIER_MEDIUM",
    "COST_TIER_NONE",
    "DefaultIngestPlanner",
    "EXECUTION_DECISION_CONDITIONAL",
    "EXECUTION_DECISION_RUN",
    "EXECUTION_DECISION_SKIP",
    "IngestMode",
    "IngestPlan",
    "IngestPlanner",
    "IngestPolicy",
    "LLM_CLASS_FAST",
    "LLM_CLASS_NONE",
    "LLM_CLASS_PREMIUM",
    "LLM_CLASS_STANDARD",
    "PlannedStep",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
    "VISION_DECISION_ENRICH",
    "VISION_DECISION_SKIP",
    "VISION_DECISION_TRIAGE",
]


# Execution-plan decisions surfaced to the frontend. Plain strings
# (rather than a StrEnum) so the JSON payload round-trips cleanly
# through audit JSONL without enum/string ambiguity. Mirrored in
# `j1.runs.models` for the REST/SSE consumer side.
EXECUTION_DECISION_RUN = "RUN"
EXECUTION_DECISION_SKIP = "SKIP"
EXECUTION_DECISION_CONDITIONAL = "CONDITIONAL"

# Coarse cost tiers shown to operators in the plan summary. Refined
# numeric estimates are out of scope; this is a UX-bucket field.
COST_TIER_NONE = "NONE"
COST_TIER_LOW = "LOW"
COST_TIER_MEDIUM = "MEDIUM"
COST_TIER_HIGH = "HIGH"

# Risk levels for surfacing "this step might lose information" hints
# (e.g. text_only policy on a scanned PDF → graph step skipped with
# `risk_level=high`).
RISK_LEVEL_LOW = "low"
RISK_LEVEL_MEDIUM = "medium"
RISK_LEVEL_HIGH = "high"

# LLM model class chosen per step. The policy default is to pick the
# cheapest class that can do the job — premium is opt-in only via
# policy=high_accuracy or caller signals (high-risk content). The
# strings round-trip through the audit log and the FE plan card.
#   `none`     no LLM call at all (e.g. text_only mode for the index
#              step uses local embeddings)
#   `fast`     small/cheap model — triage, classification, summarisation
#              of short snippets
#   `standard` production-default model — entity extraction, table
#              summarisation, normal-risk reasoning
#   `premium`  largest/strongest model — high-risk content, low-
#              confidence parses, ambiguous tables/images
LLM_CLASS_NONE = "none"
LLM_CLASS_FAST = "fast"
LLM_CLASS_STANDARD = "standard"
LLM_CLASS_PREMIUM = "premium"

# Vision LLM triage decisions surfaced on the plan. Per-document
# coarse decision; per-image entries in `IngestPlan.vision_decisions`
# carry the same vocabulary when the parser surfaces image-level
# metadata.
#   `skip`    don't call the vision LLM; text/structured output is
#             enough (default for documents with no images and for
#             documents whose images are decorative/captioned)
#   `triage`  call a fast vision pass to classify each image, then
#             decide whether to run the heavier enrichment
#   `enrich`  run the full vision enrichment (semantic description,
#             entity extraction, etc.)
VISION_DECISION_SKIP = "skip"
VISION_DECISION_TRIAGE = "triage"
VISION_DECISION_ENRICH = "enrich"


# Step names — the same strings the workflow uses for `StepResult.step`
# and the same operations the existing per-stage gates check. Keeping
# them in one place so a typo in either side surfaces immediately.
STEP_COMPILE = "compile"
STEP_ENRICH = "enrich"
STEP_GRAPH = "graph"
STEP_INDEX = "index"


class IngestPolicy(StrEnum):
    """How aggressive the planner should be.

    `auto`           planner decides from the profile alone.
    `cost_saving`    prefer skipping; only enable expensive stages
                     when the profile demands it.
    `balanced`       production default; same as auto today, distinct
                     constant so deployments can rebind it later.
    `high_accuracy`  conservative — when uncertain, enable more.
    `force_full`     enable every stage the deployment configured.
    `text_only`      only compile + index. Records warnings for
                     signals (tables / images / scanned pages) that
                     suggest the choice may lose information.
    """

    AUTO = "auto"
    COST_SAVING = "cost_saving"
    BALANCED = "balanced"
    HIGH_ACCURACY = "high_accuracy"
    FORCE_FULL = "force_full"
    TEXT_ONLY = "text_only"


class IngestMode(StrEnum):
    """Logical mode summarising what the plan will do.

    Modes are descriptive labels for operators / dashboards; the
    actual gate decisions live in `PlannedStep.enabled`. Two plans
    can have the same mode but different per-step `source` values
    (e.g. caller-supplied vs. planner-derived)."""

    TEXT_ONLY = "text_only"
    TEXT_WITH_LIGHT_ENRICHMENT = "text_with_light_enrichment"
    TABLE_AWARE = "table_aware"
    MULTIMODAL_LIGHT = "multimodal_light"
    MULTIMODAL_FULL = "multimodal_full"
    GRAPH_AWARE = "graph_aware"
    FULL_DIAGNOSTIC = "full_diagnostic"


# Mode → enabled steps mapping. Pure data; no decision logic in here.
# The planner picks a mode and reads the matching set; per-step
# `source`/`required` overrides happen elsewhere.
_MODE_ENABLED_STEPS: dict[IngestMode, frozenset[str]] = {
    IngestMode.TEXT_ONLY: frozenset({STEP_COMPILE, STEP_INDEX}),
    IngestMode.TEXT_WITH_LIGHT_ENRICHMENT: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_INDEX,
    }),
    IngestMode.TABLE_AWARE: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_INDEX,
    }),
    IngestMode.MULTIMODAL_LIGHT: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_INDEX,
    }),
    IngestMode.MULTIMODAL_FULL: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_GRAPH, STEP_INDEX,
    }),
    IngestMode.GRAPH_AWARE: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_GRAPH, STEP_INDEX,
    }),
    IngestMode.FULL_DIAGNOSTIC: frozenset({
        STEP_COMPILE, STEP_ENRICH, STEP_GRAPH, STEP_INDEX,
    }),
}


def steps_for_mode(mode: IngestMode) -> frozenset[str]:
    """Public read-only view of the mode→steps mapping. Tests and
    integration code use this rather than mutating the table."""
    return _MODE_ENABLED_STEPS[mode]


@dataclass(frozen=True)
class PlannedStep:
    """A single gate in the ingest plan.

    Two views of the same record:

      * **Workflow gate** — `enabled` / `required` / `source` /
        `reason`. The workflow consults these to decide whether to
        attempt the step and how to react on failure.
      * **Frontend execution-plan item** — `decision` / `stage` /
        `step_id` / `expected_engine` / `expected_provider` /
        `estimated_cost_tier` / `risk_level` / `warning` /
        `dependency_step_ids` / `metadata`. The UI renders these on
        the plan-review screen.

    Both views travel together so the planner emits one source of
    truth. The workflow only reads the gate fields; the API serialiser
    reads everything.

    Field hygiene: all strings are short operational values. `metadata`
    is a small structured dict — never document content."""

    name: str
    enabled: bool
    required: bool
    source: StepSource
    reason: str | None = None
    # ---- Frontend execution-plan view (defaults preserve existing
    # planner output for callers that don't consume them yet). ----
    step_id: str = ""           # "compile" / "enrich" / etc; defaults to `name`
    stage: str = ""              # canonical stage label (e.g. "COMPILE")
    decision: str = EXECUTION_DECISION_RUN
    dependency_step_ids: tuple[str, ...] = ()
    estimated_cost_tier: str = COST_TIER_NONE
    expected_engine: str | None = None
    expected_provider: str | None = None
    risk_level: str = RISK_LEVEL_LOW
    warning: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    # LLM model class the step intends to use. `none` for steps that
    # don't touch an LLM (compile, index when using local embeddings).
    # The planner picks the cheapest class that can do the job; only
    # `high_accuracy` policy or explicit caller signals upgrade to
    # `premium`. See module-level `LLM_CLASS_*` constants.
    llm_class: str = LLM_CLASS_NONE


@dataclass(frozen=True)
class IngestPlan:
    """The planner's output for a single document.

    `steps` lists every relevant step in canonical order (compile →
    enrich → graph → index). The workflow reads them in order.

    `confidence` is 0..1; below a deployment-tunable threshold the
    planner may have used an LLM fallback (`fast_llm_used=True`).

    `estimated_cost_level` is a rough bucket — "low" / "medium" /
    "high" — for cost-control dashboards. Deliberately not a numeric
    estimate; cost-prediction is its own subsystem."""

    document_id: str
    mode: IngestMode
    policy: IngestPolicy
    steps: tuple[PlannedStep, ...]
    confidence: float
    estimated_cost_level: str
    profile: DocumentProfile
    fast_llm_used: bool = False
    warnings: tuple[str, ...] = ()
    # High-level LLM/vision flags surfaced to the FE for "vision off
    # by default" / "premium opt-in" guarantees and to the Temporal
    # search-attribute layer for filtering. Computed from the per-
    # step `llm_class` values + profile signals; never store these
    # without recomputing — keeping them in sync with `steps` is a
    # planner-internal invariant.
    requires_vision: bool = False
    requires_premium_llm: bool = False
    # Per-image vision triage decisions. Empty when the parser did
    # not surface per-image metadata; the FE renders the coarse
    # `requires_vision` flag in that case. Each entry is a tiny dict
    # to keep the audit-log payload compact and forward-compatible.
    # Recognised keys: `image_id`, `decision` (skip|triage|enrich),
    # `role` (e.g. logo / diagram / chart), `score`, `reason`.
    vision_decisions: tuple[dict[str, object], ...] = ()

    def step(self, name: str) -> PlannedStep | None:
        """Lookup helper. Returns None when the planner didn't emit
        the step (typically means the deployment doesn't have it
        configured at all — e.g. graph isn't even registered)."""
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def enabled_step_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps if s.enabled)

    def skipped_step_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps if not s.enabled)


class IngestPlanner:
    """Planner interface. Implementations MUST be deterministic with
    respect to (profile, policy, available_steps, caller_overrides)
    so workflow replay produces stable plans."""

    def plan(
        self,
        profile: DocumentProfile,
        *,
        policy: IngestPolicy,
        available_steps: frozenset[str],
        caller_overrides: dict[str, bool] | None = None,
    ) -> IngestPlan:
        raise NotImplementedError


# Plain-text extensions where compile + index is always sufficient.
# Kept in lock-step with `_NATIVE_TEXT_EXTENSIONS` in
# `j1.providers.raganything._bridge` (the planner intentionally has
# no import dependency on any specific provider). Touch both files
# together when adding a new extension; the bridge file's comment
# explains why a mismatch routes plaintext through the slow MinerU
# path instead of the fast direct-feed.
_PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log",
})

# Extensions whose contents are typically non-text-extractable (need
# OCR / vision). Used as a heuristic when `text_extractable_ratio` is
# unknown.
_LIKELY_SCANNED_EXTENSIONS: frozenset[str] = frozenset({
    ".tiff", ".tif", ".bmp",
})

# Extensions that imply a tabular focus (planner biases toward
# table_aware mode when the policy permits enrichment).
_LIKELY_TABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".xls", ".xlsx", ".csv", ".ods",
})


@dataclass
class DefaultIngestPlanner(IngestPlanner):
    """Deterministic planner.

    Decision tree, applied in order:
      1. Caller overrides win. If the caller explicitly enabled or
         disabled a step, that decision sticks (source=CALLER).
      2. `force_full` policy enables every step the deployment
         supports.
      3. `text_only` policy enables only compile + index, recording
         warnings for any contradicting signals (tables / images /
         scanned).
      4. `cost_saving` policy → TEXT_WITH_LIGHT_ENRICHMENT for
         everything except clearly multimodal documents (where it
         falls back to MULTIMODAL_LIGHT).
      5. `high_accuracy` → if any optional signal is unknown, enable
         the corresponding stage anyway.
      6. `auto` / `balanced` → use the heuristics in `_pick_mode`.

    Confidence: 1.0 when every relevant signal is known; 0.7 when
    one major signal (e.g. text_extractable_ratio) is unknown; 0.5
    when most signals are unknown. The planner never emits below
    0.5 — at that point it surfaces a warning and lets the policy
    decide whether to default conservatively."""

    def plan(
        self,
        profile: DocumentProfile,
        *,
        policy: IngestPolicy,
        available_steps: frozenset[str],
        caller_overrides: dict[str, bool] | None = None,
    ) -> IngestPlan:
        overrides = dict(caller_overrides or {})
        warnings: list[str] = []

        # Step 1: pick the mode (modulo overrides applied below).
        mode = self._pick_mode(profile, policy, warnings)

        # Step 2: figure out which steps are enabled.
        mode_enabled = steps_for_mode(mode)
        steps: list[PlannedStep] = []
        for name in (STEP_COMPILE, STEP_ENRICH, STEP_GRAPH, STEP_INDEX):
            if name not in available_steps:
                # Deployment doesn't support this stage at all — skip
                # silently (no PlannedStep emitted) rather than emit
                # a "skipped, source=config" record for every plan.
                continue

            # Caller override: highest priority.
            if name in overrides:
                steps.append(_build_planned_step(
                    name=name,
                    enabled=overrides[name],
                    required=overrides[name],
                    source=StepSource.CALLER,
                    reason=None if overrides[name] else "caller disabled this step",
                    mode=mode,
                ))
                continue

            enabled = name in mode_enabled
            # Required is True for the two foundational steps when
            # enabled (compile, index); enrich/graph are optional even
            # when enabled (so a `continue_optional` policy can let
            # them fail without failing the workflow).
            required = enabled and name in (STEP_COMPILE, STEP_INDEX)
            steps.append(_build_planned_step(
                name=name,
                enabled=enabled,
                required=required,
                source=StepSource.PLANNER if name not in overrides else StepSource.CALLER,
                reason=(
                    None if enabled
                    else _skip_reason(name, mode, profile)
                ),
                mode=mode,
                policy=policy,
                profile=profile,
            ))

        # Step 3: confidence + cost level.
        confidence = self._confidence(profile)
        cost_level = self._cost_level(mode)

        # Step 4: high-level LLM/vision flags + per-image triage.
        steps_tuple = tuple(steps)
        requires_vision = _compute_requires_vision(mode, profile, steps_tuple)
        requires_premium = any(
            s.enabled and s.llm_class == LLM_CLASS_PREMIUM for s in steps_tuple
        )
        vision_decisions = _compute_vision_decisions(profile, requires_vision)

        return IngestPlan(
            document_id=profile.document_id,
            mode=mode,
            policy=policy,
            steps=steps_tuple,
            confidence=confidence,
            estimated_cost_level=cost_level,
            profile=profile,
            warnings=tuple([*profile.warnings, *warnings]),
            requires_vision=requires_vision,
            requires_premium_llm=requires_premium,
            vision_decisions=vision_decisions,
        )

    # ---- Mode selection ----------------------------------------------

    def _pick_mode(
        self,
        profile: DocumentProfile,
        policy: IngestPolicy,
        warnings: list[str],
    ) -> IngestMode:
        if policy == IngestPolicy.FORCE_FULL:
            return IngestMode.FULL_DIAGNOSTIC

        if policy == IngestPolicy.TEXT_ONLY:
            # Caller explicitly said text-only, but record warnings if
            # the profile suggests they're losing content.
            if profile.has_tables is True:
                warnings.append(
                    "text_only policy but profile reports tables present"
                )
            if profile.has_images is True:
                warnings.append(
                    "text_only policy but profile reports images present"
                )
            if profile.has_scanned_pages is True:
                warnings.append(
                    "text_only policy but profile reports scanned pages — "
                    "OCR will not run"
                )
            return IngestMode.TEXT_ONLY

        # Plain text always goes TEXT_ONLY regardless of policy
        # (other than force_full handled above) — running enrichment
        # on a 10-byte .txt is the over-processing this whole
        # subsystem is built to avoid.
        if profile.extension in _PLAIN_TEXT_EXTENSIONS:
            return IngestMode.TEXT_ONLY

        # Likely scanned (no extractable text) → MULTIMODAL_FULL,
        # subject to high_accuracy / cost_saving overrides below.
        if (
            profile.has_scanned_pages is True
            or profile.text_extractable_ratio is not None
            and profile.text_extractable_ratio < 0.1
            or profile.extension in _LIKELY_SCANNED_EXTENSIONS
        ):
            if policy == IngestPolicy.COST_SAVING:
                # Cost-saving still skips graph for scanned-only docs,
                # but keeps enrich + index so the doc is searchable.
                return IngestMode.MULTIMODAL_LIGHT
            return IngestMode.MULTIMODAL_FULL

        if profile.has_tables is True or profile.extension in _LIKELY_TABLE_EXTENSIONS:
            return IngestMode.TABLE_AWARE

        if profile.has_images is True:
            return IngestMode.MULTIMODAL_LIGHT

        # Default: text with metadata enrichment but no graph.
        # Graph extraction is intentionally NOT auto-enabled — it's
        # the most expensive optional stage, and operators must
        # opt in via policy=force_full or caller-supplied
        # graphBuilderKind.
        if policy == IngestPolicy.HIGH_ACCURACY:
            return IngestMode.GRAPH_AWARE
        if policy == IngestPolicy.COST_SAVING:
            return IngestMode.TEXT_ONLY
        return IngestMode.TEXT_WITH_LIGHT_ENRICHMENT

    # ---- Confidence + cost level -------------------------------------

    def _confidence(self, profile: DocumentProfile) -> float:
        """Crude but deterministic. Counts how many of the four
        major signals are populated; remaps to 0.5..1.0."""
        signals = (
            profile.text_extractable_ratio,
            profile.has_images,
            profile.has_tables,
            profile.has_scanned_pages,
        )
        known = sum(1 for s in signals if s is not None)
        # 0 known → 0.5; all known → 1.0.
        return 0.5 + (known / len(signals)) * 0.5

    def _cost_level(self, mode: IngestMode) -> str:
        """Coarse bucketing. Refined when actual cost data is wired
        through `LLMUsage` aggregation."""
        if mode in (IngestMode.TEXT_ONLY,):
            return "low"
        if mode in (
            IngestMode.TEXT_WITH_LIGHT_ENRICHMENT,
            IngestMode.TABLE_AWARE,
            IngestMode.MULTIMODAL_LIGHT,
        ):
            return "medium"
        return "high"


# Mapping from step name to canonical UI labels. Defined here so the
# planner stays the single source of truth for what each stage is
# called in the execution-plan view.
_STAGE_LABEL: dict[str, str] = {
    STEP_COMPILE: "COMPILE",
    STEP_ENRICH: "ENRICH",
    STEP_GRAPH: "GRAPH",
    STEP_INDEX: "INDEX",
}

# Per-step cost tier. Matches the coarse buckets shown in the plan
# UI; absolute spend tracking lives in the cost-recorder subsystem.
_STEP_COST_TIER: dict[str, str] = {
    STEP_COMPILE: COST_TIER_MEDIUM,   # mineru / vendor parse
    STEP_ENRICH: COST_TIER_MEDIUM,    # vision LLM calls
    STEP_GRAPH: COST_TIER_HIGH,       # entity/relation extraction LLM calls
    STEP_INDEX: COST_TIER_LOW,        # local embedding, vector index write
}

# Cross-step dependencies. Compile must run first; everything else
# depends on its artifacts. Index is global — it consumes whatever
# enrich and graph produced.
_STEP_DEPS: dict[str, tuple[str, ...]] = {
    STEP_COMPILE: (),
    STEP_ENRICH: (STEP_COMPILE,),
    STEP_GRAPH: (STEP_COMPILE, STEP_ENRICH),
    STEP_INDEX: (STEP_COMPILE,),
}


def _build_planned_step(
    *,
    name: str,
    enabled: bool,
    required: bool,
    source: StepSource,
    reason: str | None,
    mode: IngestMode,
    policy: IngestPolicy = IngestPolicy.AUTO,
    profile: DocumentProfile | None = None,
) -> PlannedStep:
    """Construct a `PlannedStep` populated with both the workflow-gate
    fields and the frontend execution-plan fields. Centralised so the
    UI-facing metadata (stage label, cost tier, dependencies, llm
    class) stays in sync with the gate decision."""
    decision = (
        EXECUTION_DECISION_RUN if enabled else EXECUTION_DECISION_SKIP
    )
    return PlannedStep(
        name=name,
        enabled=enabled,
        required=required,
        source=source,
        reason=reason,
        step_id=name,
        stage=_STAGE_LABEL.get(name, name.upper()),
        decision=decision,
        dependency_step_ids=_STEP_DEPS.get(name, ()),
        estimated_cost_tier=_STEP_COST_TIER.get(name, COST_TIER_NONE),
        risk_level=_risk_level_for_skip(name, enabled, mode),
        llm_class=_pick_llm_class(name, enabled, mode, policy, profile),
    )


def _pick_llm_class(
    step: str,
    enabled: bool,
    mode: IngestMode,
    policy: IngestPolicy,
    profile: DocumentProfile | None,
) -> str:
    """Choose an LLM class for a step. Skipped steps always = `none`.

    Defaults err cheap. Premium is opt-in: only `high_accuracy` policy
    or known-low-confidence parser output upgrades a step to premium."""
    if not enabled:
        return LLM_CLASS_NONE
    # Stages that never call an LLM regardless of mode.
    if step in (STEP_COMPILE, STEP_INDEX):
        return LLM_CLASS_NONE
    if policy == IngestPolicy.FORCE_FULL or policy == IngestPolicy.HIGH_ACCURACY:
        # Operator explicitly asked for the strongest path.
        return LLM_CLASS_PREMIUM
    if policy == IngestPolicy.COST_SAVING:
        # Cost-saving uses the cheapest LLM class for any LLM-bearing
        # step that the mode demands; if the mode has no LLM step the
        # planner already disabled it above.
        return LLM_CLASS_FAST
    # Per-step defaults for `auto` / `balanced`.
    if step == STEP_ENRICH:
        if mode == IngestMode.TEXT_ONLY:
            return LLM_CLASS_NONE
        if mode == IngestMode.TEXT_WITH_LIGHT_ENRICHMENT:
            return LLM_CLASS_FAST
        return LLM_CLASS_STANDARD
    if step == STEP_GRAPH:
        # Graph extraction is the most expensive optional step; if the
        # parser flagged the document as low-confidence (e.g. lots of
        # scanned pages with mixed extraction), upgrade to premium so
        # entity extraction has a fighting chance.
        if profile is not None and profile.has_scanned_pages is True:
            return LLM_CLASS_PREMIUM
        return LLM_CLASS_STANDARD
    return LLM_CLASS_NONE


def _compute_requires_vision(
    mode: IngestMode,
    profile: DocumentProfile,
    steps: tuple[PlannedStep, ...],
) -> bool:
    """Coarse 'does this run need a vision LLM?' flag.

    Vision is OFF by default. It flips on only when:
      * the mode is multimodal (operator/profile chose it);
      * the profile reports scanned pages (only OCR/vision can recover
        text);
      * the deployment forced FULL_DIAGNOSTIC.
    A document that merely contains images does NOT trigger vision —
    that's the spec's "vision off by default" guarantee. Per-image
    triage in `vision_decisions` handles the fine-grained case."""
    enabled_step_names = {s.name for s in steps if s.enabled}
    if STEP_ENRICH not in enabled_step_names:
        # No enrich step → no vision call possible.
        return False
    if mode in (
        IngestMode.MULTIMODAL_LIGHT,
        IngestMode.MULTIMODAL_FULL,
        IngestMode.FULL_DIAGNOSTIC,
    ):
        return True
    if profile.has_scanned_pages is True:
        return True
    return False


def _compute_vision_decisions(
    profile: DocumentProfile,
    requires_vision: bool,
) -> tuple[dict[str, object], ...]:
    """Per-image triage decisions surfaced on the plan.

    When the parser populated `profile.images` (see the raganything
    bridge's `_build_content_manifest`), each entry already carries
    a `decision` / `role` / `score` / `reason` set by file-level
    heuristics. We pass it through verbatim so the FE plan card can
    render per-image badges.

    When the doc-level `requires_vision=False` (e.g. text-only mode)
    we override every per-image decision to `skip` — there's no point
    triaging individual images for a run that won't call the vision
    LLM at all. The original heuristic stays in `_original_decision`
    so future replans can reconsider."""
    if not profile.images:
        return ()
    out: list[dict[str, object]] = []
    for entry in profile.images:
        decision = entry.get("decision", VISION_DECISION_TRIAGE)
        if not requires_vision and decision != VISION_DECISION_SKIP:
            out.append({
                **entry,
                "decision": VISION_DECISION_SKIP,
                "_original_decision": decision,
                "reason": "vision LLM disabled at document level",
            })
        else:
            out.append(dict(entry))
    return tuple(out)


def _risk_level_for_skip(
    step: str, enabled: bool, mode: IngestMode,
) -> str:
    """Tag the operational risk of skipping a given step.

    Skipping `graph` is low-risk (it's an optional augmentation).
    Skipping `enrich` is medium for table/multimodal modes (loses
    structured data). Skipping `compile` would be catastrophic, but
    the planner never emits that — defensive default = high."""
    if enabled:
        return RISK_LEVEL_LOW
    if step == STEP_COMPILE:
        return RISK_LEVEL_HIGH
    if step == STEP_ENRICH and mode in (
        IngestMode.TABLE_AWARE,
        IngestMode.MULTIMODAL_LIGHT,
        IngestMode.MULTIMODAL_FULL,
    ):
        return RISK_LEVEL_MEDIUM
    if step == STEP_INDEX:
        # Skipping index breaks searchability.
        return RISK_LEVEL_HIGH
    return RISK_LEVEL_LOW


def _skip_reason(step: str, mode: IngestMode, profile: DocumentProfile) -> str:
    """Human-readable reason for `PlannedStep.reason` when a step is
    disabled. Generic — no client / domain language."""
    if step == STEP_GRAPH:
        return (
            f"mode {mode.value} does not include graph extraction; "
            "graph requires explicit policy=force_full or caller-supplied kind"
        )
    if step == STEP_ENRICH:
        return f"mode {mode.value} does not include enrichment"
    if step == STEP_INDEX:
        return f"mode {mode.value} does not include indexing"
    if step == STEP_COMPILE:
        return f"mode {mode.value} does not include compilation"
    return f"step disabled by mode {mode.value}"
