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
    "PlannedStep",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
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
# Mirrors `_NATIVE_TEXT_EXTENSIONS` in the raganything bridge — kept
# in sync at review time, not via import (the planner intentionally
# has no dependency on any specific provider).
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
            ))

        # Step 3: confidence + cost level.
        confidence = self._confidence(profile)
        cost_level = self._cost_level(mode)

        return IngestPlan(
            document_id=profile.document_id,
            mode=mode,
            policy=policy,
            steps=tuple(steps),
            confidence=confidence,
            estimated_cost_level=cost_level,
            profile=profile,
            warnings=tuple([*profile.warnings, *warnings]),
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
) -> PlannedStep:
    """Construct a `PlannedStep` populated with both the workflow-gate
    fields and the frontend execution-plan fields. Centralised so the
    UI-facing metadata (stage label, cost tier, dependencies) stays
    in sync with the gate decision."""
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
    )


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
