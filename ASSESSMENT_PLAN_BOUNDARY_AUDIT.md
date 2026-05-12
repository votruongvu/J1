# Assessment Plan ↔ Compile Boundary Audit — 2026-05-12

## TL;DR

**The backend contract separation is correct. The bug is exclusively on the
read/UI side.**

- Backend: an `initial_execution_plan` artifact is built and persisted
  PRE-compile by the `build_initial_execution_plan` activity. Its
  payload carries the AssessmentPlan as `compile_plan`. The same plan
  is then passed into the RAGAnything compile call.
- Frontend: `AssessmentPlanPanel.tsx` reads from `compile_strategy_report`,
  a POST-compile artifact. Its own header docstring says so. The panel
  therefore stays in "loading" / "missing" until compile finishes.
- The correct pre-compile UI consumer (`InitialExecutionPlanPanel.tsx`)
  STILL EXISTS but was removed from the run-detail page three days ago
  on operator request, leaving no UI window onto the pre-compile artifact.

## Evidence: backend separation is correct

### 1. Build site (pre-compile, in workflow)

[src/j1/orchestration/workflows/project_processing.py:3085-3114](src/j1/orchestration/workflows/project_processing.py#L3085-L3114):

```python
build_result = await workflow.execute_activity_method(
    ProcessingActivities.build_initial_execution_plan,
    BuildInitialExecutionPlanInput(...),
    start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
    retry_policy=DEFAULT_RETRY.to_temporal(),
)
plan_payload_raw = (
    getattr(build_result, "plan_payload", None)
    if build_result is not None else None
)
if plan_payload_raw:
    initial_plan_payload = dict(plan_payload_raw)
    assessment_payload = (
        initial_plan_payload.get("compile_plan") or None
    )
else:
    # Legacy fallback
    fallback = DefaultAssessmentPlanner().assess(profile)
    assessment_payload = fallback.to_payload()
```

This runs in the document loop BEFORE the compile attempt loop. The
`assessment_payload` (AssessmentPlan) is in hand at this point.

### 2. Persistence site (pre-compile)

[src/j1/orchestration/activities/processing.py:1414-1424](src/j1/orchestration/activities/processing.py#L1414-L1424):

```python
try:
    record = self._processing.persist_initial_execution_plan(
        ctx,
        run_id=input.run_id,
        document_id=input.document_id,
        payload=plan_payload,
        actor=input.actor,
    )
    artifact_id = record.artifact_id
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
```

The artifact is written inside the activity, before it returns to the
workflow. By the time `build_initial_execution_plan` resolves, the
`initial_execution_plan` artifact is on disk and queryable via the
REST artifact-listing endpoint.

### 3. Payload contents (AssessmentPlan is fully inside)

[src/j1/processing/initial_execution_plan.py:124-141](src/j1/processing/initial_execution_plan.py#L124-L141):

```python
def to_payload(self) -> dict[str, Any]:
    return {
        "schema_version": ...,
        "document_id": ...,
        "domain_profile_id": ...,
        "enrichment_policy": ...,
        "require_enrichment_success": ...,
        "candidate_modules": ...,
        "cheap_signals": ...,
        "resource_hints": ...,
        "reasons": ...,
        "warnings": ...,
        "compile_plan": (
            self.compile_plan.to_payload()
            if self.compile_plan else None
        ),
    }
```

`compile_plan` is the full AssessmentPlan (mode, confidence,
document_type, complexity, fallback_policy, reason,
required_capabilities, optional_capabilities, risk_flags).

### 4. Plan is then passed into compile

The same `assessment_payload` is fed into the compile activity as
`assessment_plan_payload` in the per-attempt loop further down in
[project_processing.py](src/j1/orchestration/workflows/project_processing.py#L3275)
(line 3275). The compile activity reconstructs an `AssessmentPlan`
from this payload and hands it to the RAGAnything adapter
([processing.py:625-635](src/j1/orchestration/activities/processing.py#L625-L635)),
which forwards it into `RAGAnythingCompileRequest.assessment_plan`.

### 5. CompileStrategyReport is built POST-compile (also correct)

[project_processing.py:3486](src/j1/orchestration/workflows/project_processing.py#L3486):
the `compile_strategy_report` artifact is persisted only AFTER the
per-document compile attempt loop completes. Its content includes
the per-attempt audit + final quality verdict + the final
AssessmentPlan that was active when compile succeeded (which may
differ from the initial one if mode-escalation retry happened).

This is a different, post-compile artifact. The data overlap is
deliberate: the report quotes BOTH the initial AssessmentPlan and
the final one so operators can see escalation. But it should not be
the source of truth for the pre-compile Assessment Plan panel — by
definition, it isn't available until compile finishes.

## Evidence: the bug is in the FE

### 1. AssessmentPlanPanel reads from the wrong artifact

[frontend/src/pages/run-detail/AssessmentPlanPanel.tsx:6-9](frontend/src/pages/run-detail/AssessmentPlanPanel.tsx#L6-L9):

> Source of truth: the `compile_strategy_report` artifact. The
> AssessmentPlan is built pre-compile (cheap deterministic profile
> + `DefaultAssessmentPlanner`) and persisted as part of the
> report once compile finishes.

The docstring documents the bug. Lines 80-81 confirm it: the panel
calls `client.listRunArtifacts(runId, { kind: COMPILE_STRATEGY_REPORT_KIND })`.

While compile is running, this list is empty, so the panel falls
through to its `missing` placeholder ("No assessment data available
for this run yet"). The data the panel exists to show is sitting in
the `initial_execution_plan` artifact, persisted seconds earlier,
unread.

### 2. The correct consumer was removed from the page

`InitialExecutionPlanPanel.tsx` still exists at
[frontend/src/pages/run-detail/InitialExecutionPlanPanel.tsx](frontend/src/pages/run-detail/InitialExecutionPlanPanel.tsx).
It fetches `getRunInitialExecutionPlan(runId)` — the REST endpoint
backed by the pre-compile artifact — and renders domain / policy /
candidate modules / reasons / warnings. It does NOT render the
`compile_plan` sub-payload (mode / confidence / capabilities), but
the typed payload (`InitialExecutionPlanPayload.compile_plan`) does
carry it.

This panel was removed from `RunDetailPage.tsx` three days ago at
operator request as "unused". With it removed, no UI consumer reads
the pre-compile artifact.

The data fetch on `RunDetailPage.tsx` for `initialPlan` is still
present and the `initialPlan` state is still consumed by
`PrimaryStatusPanel` as a fallback signal. But the visual Assessment
Plan surface depends on `compile_strategy_report` exclusively.

### 3. No backend "post-compile data leaking into assessment_plan"

I confirmed the user's hypotheses are not happening on the backend:

- `assessment_plan` (the AssessmentPlan dataclass) does NOT carry
  `detected_images`, `detected_tables`, `chunks_count`, or any
  parser/graph/index output. It carries mode + confidence + capabilities
  + reason. [assessment.py:1-200](src/j1/processing/assessment.py).
- The `initial_execution_plan` artifact is built BEFORE compile, so
  it CAN'T contain compile_result fields.
- The `compile_strategy_report` does include the AssessmentPlan
  alongside per-attempt compile data, but that's a deliberate
  presentation choice for the post-compile report — it's not
  mutating the assessment plan itself.
- `_build_plan` (the deferred post-compile method called out in
  memory) does not exist in the active codebase. Same for
  `_apply_post_compile_planning`. The regression test
  `tests/test_compile_first_no_split.py` already pins their absence.

No data corruption is happening; the contracts ARE separate. The UI
is just reading from the wrong shelf.

## Fix plan

Narrow, no architecture change. Three commits:

### Fix 1 — point `AssessmentPlanPanel` at the pre-compile artifact (PRIMARY)

In `AssessmentPlanPanel.tsx`:

- Replace the `listRunArtifacts({kind: COMPILE_STRATEGY_REPORT_KIND})`
  fetch with `client.getRunInitialExecutionPlan(runId)`.
- Extract the `compile_plan` sub-payload as the AssessmentPlan source
  (mode / confidence / capabilities / reason / etc.).
- Use `domain_profile_id` + `enrichment_policy` + `require_enrichment_success`
  + `candidate_modules` directly from the top-level payload — these
  are the parts the InitialExecutionPlanPanel rendered.
- KEEP a secondary fetch of `compile_strategy_report` for the
  POST-compile enrichments the panel still surfaces: mode escalation
  hint, final compile mode, extraction evidence, plan warnings,
  unhandled capabilities. Merge into the same panel under a clearly
  separate "After compile" section that only renders once that
  artifact is present.
- Update the panel docstring to reflect the new dual-source
  separation.
- Update the `data-testid="assessment-plan-source"` source label —
  drop the "fallback (no AssessmentPlan attached)" branch entirely
  because the pre-compile artifact ALWAYS carries the plan when it's
  present.

This makes the Assessment Plan panel populate within a second of
upload (as soon as the build_initial_execution_plan activity
finishes), independently of compile status.

### Fix 2 — delete the unused `InitialExecutionPlanPanel.tsx`

With AssessmentPlanPanel reading the pre-compile artifact directly,
the standalone InitialExecutionPlanPanel duplicates the same data.
The operator already asked for it to be removed from the page; this
finishes the removal:

- Delete `frontend/src/pages/run-detail/InitialExecutionPlanPanel.tsx`.
- Drop `InitialExecutionPlanPanel` from the regression test list at
  `frontend/src/lib/__tests__/vocabulary.test.ts:94`.

Keep `getRunInitialExecutionPlan` (the API client method) and the
REST endpoint — `AssessmentPlanPanel` and `PrimaryStatusPanel` both
still need them.

### Fix 3 — regression tests

New backend test `tests/test_assessment_plan_precompile_contract.py`:

1. `test_initial_execution_plan_persisted_before_compile_invocation` —
   patch the worker activities; record the order they're called in;
   assert `build_initial_execution_plan` (which persists the artifact)
   completes before any `compile` activity starts.
2. `test_initial_execution_plan_carries_no_postcompile_fields` —
   load a real `InitialExecutionPlan.to_payload()` and assert keys
   like `chunks_count`, `detected_images`, `detected_tables`,
   `extracted_text_chars`, `graph_artifact_ids` are NOT present.
3. `test_assessment_plan_passed_into_compile_unchanged` — instantiate
   an AssessmentPlan, run `_build_assessment_plan_payload`, hand it
   to the compile activity stub, assert the reconstructed plan
   matches the original.

New FE test `frontend/src/pages/run-detail/__tests__/assessment-plan-precompile.test.tsx`:

1. `renders pre-compile AssessmentPlan when getRunInitialExecutionPlan resolves` —
   mock the client to return an `initial_execution_plan` payload with
   `compile_plan` populated; mount the panel; assert mode + confidence
   + capabilities are visible.
2. `does not require compile_strategy_report for primary content` —
   same as above but the strategy-report fetch returns 404; the
   primary panel content still renders.
3. `merges post-compile extraction evidence when available` — both
   fetches succeed; the "after compile" section appears with parser
   method / detected_content_types.

## Out of scope

- No changes to the AssessmentPlan dataclass shape.
- No changes to the `compile_strategy_report` shape.
- No backend activity rewrites.
- No PostCompileEvaluation contract — the existing
  `compile_strategy_report` (per-attempt audit + quality verdict)
  and the existing `post_compile_enrich_plan` artifact already cover
  the post-compile observation surface.
- No `_build_plan` or `_apply_post_compile_planning` — those are
  retired/deferred per memory and not in the active code path.

## Effort estimate

- Fix 1: ~80 lines in `AssessmentPlanPanel.tsx`, ~10 lines in CSS, ~30
  lines in the existing test file.
- Fix 2: ~5 lines (delete file + update one test list).
- Fix 3: ~150 lines of new tests across BE + FE.

Total: one PR, mostly FE, low risk.
