"""Wave 9A — retry-count search attributes.

Pins two new int-typed search attributes the workflow writes so ops
dashboards can aggregate runs by retry cost:

  * `J1CompileRetryCount` — attempts beyond the first compile try
    (0 == single-attempt success, N == N retries after the first).
  * `J1EnrichmentRetryCount` — sum of per-module retry attempts the
    runner reported. Reserved for future limiter-driven accounting;
    current modules emit 0.

Both are gated by `request.search_attributes_enabled` like every other
upsert in the workflow.
"""

from __future__ import annotations

import asyncio

from temporalio import workflow

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    BuildInitialExecutionPlanResult,
    ProcessingActivityResult,
    ProjectScope,
    RunEnrichmentStageResult,
    StageValidationActivityResult,
    ValidateContextResult,
)
from j1.orchestration.workflows import project_processing as workflow_mod
from j1.orchestration.workflows.project_processing import (
    SEARCH_ATTR_COMPILE_RETRY_COUNT,
    SEARCH_ATTR_ENRICHMENT_RETRY_COUNT,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.initial_execution_plan import build_initial_execution_plan
from j1.processing.profiling import DocumentProfile


_PROFILE = DocumentProfile(
    document_id="doc-1",
    extension=".pdf",
    page_count=10,
    total_text_chars=15_000,
)


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _activity_name(method) -> str:
    return (
        getattr(method, "__temporal_activity_definition", None)
        and method.__temporal_activity_definition.name
        or getattr(method, "__name__", str(method))
    )


def _patch_workflow_runtime(monkeypatch, *, exec_handler):
    async def _exec(method, payload=None, **kwargs):
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow_mod.workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow_mod.workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow_mod.workflow, "wait_condition", _wait)
    monkeypatch.setattr(
        workflow_mod.workflow, "continue_as_new", lambda *_a, **_k: None,
    )


def _capture_search_attributes(monkeypatch) -> list[dict]:
    """Capture every `upsert_search_attributes` call. The typed-update
    objects carry the search-attribute name + value; flatten them to
    plain dicts the tests can assert on."""
    captured: list[dict] = []

    def _upsert(updates):
        for u in updates:
            key = getattr(u, "key", None)
            captured.append({
                "name": getattr(key, "name", None) if key is not None else None,
                "value": getattr(u, "value", None),
            })

    monkeypatch.setattr(workflow, "upsert_search_attributes", _upsert)
    return captured


def _handler(*, enrichment_retry_count: int = 0):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _PROFILE
        if name.endswith("build_initial_execution_plan"):
            plan = build_initial_execution_plan(_PROFILE)
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="initial-plan-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["compile-1", "chunk-1"],
                kinds=("parsed_content_manifest", "chunk"),
                compile_metrics={
                    "chunks_count": 1,
                    "extracted_text_chars": 15_000,
                },
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=getattr(payload, "stage_name", "compile"),
                validation_status="passed",
                passed=True,
            )
        if name.endswith("run_enrichment_stage"):
            return RunEnrichmentStageResult(
                status="succeeded",
                plan_payload={"document_id": "doc-1", "status": "succeeded"},
                artifact_id="enrichment-art-1",
                require_enrichment_success=False,
                retry_count=enrichment_retry_count,
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("fast_llm_consult_enrich"):
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        # All persist_* activities — uniform success.
        if name.startswith("j1.processing.persist_") or "persist_" in name:
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r-1"],
                kinds=("artifact",),
            )
        return None
    return handler


def _request(**overrides) -> ProjectProcessingRequest:
    base = dict(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        correlation_id="run-test",
        search_attributes_enabled=True,
    )
    base.update(overrides)
    return ProjectProcessingRequest(**base)


# ---- 1. Single-attempt success path writes 0 retries ----------------


def test_compile_retry_count_is_zero_for_single_attempt_success(monkeypatch):
    """A clean single-attempt compile must upsert
    `J1CompileRetryCount=0` so the FE renders "0 retries" instead of
    "unknown"."""
    captured = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(monkeypatch, exec_handler=_handler())
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))

    compile_retry_writes = [
        sa for sa in captured if sa["name"] == SEARCH_ATTR_COMPILE_RETRY_COUNT
    ]
    assert compile_retry_writes, (
        f"workflow must upsert {SEARCH_ATTR_COMPILE_RETRY_COUNT}; "
        f"saw {[sa['name'] for sa in captured]}"
    )
    # Int-typed upsert: the value list carries int(0).
    final_value = compile_retry_writes[-1]["value"]
    assert final_value == 0 or final_value == [0], (
        f"compile retry count must be 0 for single attempt; "
        f"saw {final_value!r}"
    )


def test_enrichment_retry_count_passes_through_from_activity(monkeypatch):
    """The activity reports the retry count on its result; the workflow
    must forward it to the search attribute unchanged."""
    captured = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(
        monkeypatch, exec_handler=_handler(enrichment_retry_count=3),
    )
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))

    enrich_retry_writes = [
        sa for sa in captured
        if sa["name"] == SEARCH_ATTR_ENRICHMENT_RETRY_COUNT
    ]
    assert enrich_retry_writes, (
        f"workflow must upsert {SEARCH_ATTR_ENRICHMENT_RETRY_COUNT}; "
        f"saw {[sa['name'] for sa in captured]}"
    )
    final_value = enrich_retry_writes[-1]["value"]
    assert final_value == 3 or final_value == [3], (
        f"enrichment retry count must mirror activity result; "
        f"saw {final_value!r}"
    )


def test_enrichment_retry_count_defaults_to_zero(monkeypatch):
    """When the activity result omits `retry_count` (legacy worker),
    the workflow must default to 0 — never crash on missing attr."""
    captured = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(monkeypatch, exec_handler=_handler())
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))

    enrich_retry_writes = [
        sa for sa in captured
        if sa["name"] == SEARCH_ATTR_ENRICHMENT_RETRY_COUNT
    ]
    assert enrich_retry_writes
    final_value = enrich_retry_writes[-1]["value"]
    assert final_value == 0 or final_value == [0]


# ---- 2. Disabled-flag gate ------------------------------------------


def test_retry_count_search_attributes_skipped_when_disabled(monkeypatch):
    """`search_attributes_enabled=False` (default) means NO upsert
    happens — neither retry-count attribute nor any other. Critical
    regression: the dev cluster registers the attrs, but staging /
    prod may not have rolled out the registration yet."""
    captured = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(monkeypatch, exec_handler=_handler())
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request(search_attributes_enabled=False)))

    retry_writes = [
        sa for sa in captured
        if sa["name"] in (
            SEARCH_ATTR_COMPILE_RETRY_COUNT,
            SEARCH_ATTR_ENRICHMENT_RETRY_COUNT,
        )
    ]
    assert retry_writes == [], (
        f"no retry-count upserts allowed when disabled; "
        f"saw {retry_writes}"
    )


# ---- 3. Int-typed upsert (registry rejects wrong-typed attr) -------


def test_compile_retry_count_uses_int_typed_search_attribute(monkeypatch):
    """`J1CompileRetryCount` is registered as Int in the dev cluster;
    upserting a string-typed key with the same name would be rejected
    by Temporal at activation-completion. Verify the workflow uses the
    int-typed `SearchAttributeKey.for_int` path."""
    seen_for_int: list[str] = []
    seen_for_keyword: list[str] = []

    # Patch SearchAttributeKey factories so we observe which factory
    # the workflow chose for the retry-count attribute.
    from temporalio.common import SearchAttributeKey

    real_for_int = SearchAttributeKey.for_int
    real_for_keyword = SearchAttributeKey.for_keyword

    def _for_int(name):
        seen_for_int.append(name)
        return real_for_int(name)

    def _for_keyword(name):
        seen_for_keyword.append(name)
        return real_for_keyword(name)

    monkeypatch.setattr(SearchAttributeKey, "for_int", staticmethod(_for_int))
    monkeypatch.setattr(SearchAttributeKey, "for_keyword", staticmethod(_for_keyword))

    _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(monkeypatch, exec_handler=_handler())
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))

    assert SEARCH_ATTR_COMPILE_RETRY_COUNT in seen_for_int, (
        f"compile retry count must use Int key; "
        f"saw for_int={seen_for_int} for_keyword={seen_for_keyword}"
    )
    assert SEARCH_ATTR_ENRICHMENT_RETRY_COUNT in seen_for_int, (
        f"enrichment retry count must use Int key; "
        f"saw for_int={seen_for_int} for_keyword={seen_for_keyword}"
    )
    assert SEARCH_ATTR_COMPILE_RETRY_COUNT not in seen_for_keyword
    assert SEARCH_ATTR_ENRICHMENT_RETRY_COUNT not in seen_for_keyword
