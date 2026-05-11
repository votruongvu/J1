"""end-to-end test for the worker enrichment wiring.

Drives the production-like composition path:

 bootstrap-shape inputs
 → ProcessingActivities construction
 → run_enrichment_stage activity
 → fake text + vision analysis clients
 → typed EnrichmentResult
 → final_ingestion_report stage outcomes

Proves the new wiring path threads text/vision/limiter through the
activity into the legacy-compatible EnrichmentModule adapters AND
that those adapters skip cleanly with documented reasons when the
clients are not supplied.

This test stays at the ACTIVITY layer (mirrors
`test_enrichment_stage_integration.py`) — it doesn't spin a real
Temporal worker. The production-composition concern this slice
guards is "does the activity receive the three deps the adapters
need" — not "does Temporal route them".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.cost.recorder import DefaultCostRecorder
from j1.cost.sink import JsonlCostSink
from j1.orchestration.activities.payloads import (
    ProjectScope,
    RunEnrichmentStageInput,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.processing.compile_result import NormalizedCompileResult
from j1.processing.enrich_assessment import (
    EnrichRecommendation,
    PostCompileEnrichPlan,
)
from j1.processing.enrichment_clients import (
    PerImageVisionAdapter,
    TextLLMClientAdapter,
    VisionImagePayload,
)
from j1.processing.final_ingestion_report import (
    ReportSourceInputs,
    build_final_ingestion_report,
)
from j1.processing.results import ARTIFACT_KIND_ENRICHMENT_RESULT
from j1.processing.service import ProcessingService


# ---- Fakes for the analysis clients --------------------------------


class _FakeUsage:
    def __init__(self, model="fake", input_tokens=10, output_tokens=20,
                 provider="fake-vendor"):
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextClient:
    """Returns a stable response so the test can assert on the
 typed projection. Mirrors the production `TextLLMClient.extract`
 signature."""

    def __init__(self, response=None):
        self._response = response or {}
        self.calls: list[tuple] = []

    def extract(self, prompt, schema, *, metadata=None):
        self.calls.append((prompt[:80], dict(metadata or {})))
        return (self._response, _FakeUsage())


class _FakeVisionLLMClient:
    """Production-shape vision client: per-image bytes input, text
 response. Used through the `PerImageVisionAdapter`."""

    def __init__(self, response="a generic image"):
        self._response = response
        self.calls: list[dict] = []

    def analyze_image(self, image, *, prompt, media_type=None, metadata=None):
        self.calls.append({
            "bytes_len": len(image), "prompt": prompt[:80],
            "media_type": media_type,
        })
        return self._response, _FakeUsage()


class _RecordingLimiter:
    """Fake limiter that records every call. Used to assert the
 bootstrap-supplied limiter actually reaches each adapter."""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, callable_, *args, metadata=None):
        self.calls.append({"metadata": dict(metadata or {})})
        return callable_(*args)


# ---- Activity-level construction -----------------------------------


def _activities(
    workspace,
    artifact_registry,
    *,
    text_client: object | None = None,
    vision_client: object | None = None,
    llm_call_limiter: object | None = None,
):
    """Construct `ProcessingActivities` the same way the deployment
 wiring layer does — without spinning the wider worker. Keeps
 the composition surface this test pins minimal."""
    sources_dir = workspace
    audit = DefaultAuditRecorder(JsonlAuditSink(workspace))
    cost = DefaultCostRecorder(JsonlCostSink(workspace))
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifact_registry,
        audit=audit, cost=cost,
    )
    # Sources registry isn't exercised here but the constructor needs
    # one — supply a thin stand-in.
    class _NullSources:
        def get(self, ctx, doc_id):
            raise LookupError(doc_id)

    return ProcessingActivities(
        processing=processing,
        sources=_NullSources(),
        artifacts=artifact_registry,
        enrichment_text_client=text_client,
        enrichment_vision_client=vision_client,
        enrichment_llm_call_limiter=llm_call_limiter,
    )


def _compile_payload(*, chunks=5, tables=0, images=0):
    """Produce a typed `NormalizedCompileResult.to_payload` dict
 matching the activity input shape."""
    return NormalizedCompileResult(
        document_id="doc-1",
        status="succeeded",
        raw_artifact_refs=("raw-1",),
        chunks_count=chunks,
        extracted_text_chars=10_000,
        detected_tables=tuple(
            __import__("j1.processing.compile_result",
                       fromlist=["DetectedTable"]).DetectedTable(
                table_id=f"t-{i}", page=1) for i in range(tables)
        ),
        detected_images=tuple(
            __import__("j1.processing.compile_result",
                       fromlist=["DetectedImage"]).DetectedImage(
                image_id=f"i-{i}", page=1) for i in range(images)
        ),
    ).to_payload()


def _enrich_plan_payload():
    return PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
    ).to_payload()


# ---- 1. Adapters skip when no clients are wired (production-safe) --


def test_activity_constructed_without_clients_skips_all_legacy_modules(
    workspace, artifact_registry, ctx,
):
    """Fresh deployment without LLM credentials: bootstrap passes
 None for text/vision/limiter; the adapters must construct
 cleanly + skip per-run with documented reasons. The final
 report's enrichment_summary surfaces the missing-client skips
 as SKIPPED module outcomes — never silent."""
    activity = _activities(workspace, artifact_registry)
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-1",
        document_id="doc-1",
        compile_result_payload=_compile_payload(tables=2, images=2),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    plan = result.plan_payload
    outcomes = plan.get("module_outcomes") or []
    # The four legacy-compatible adapters must appear with status=skipped.
    skipped_ids = {
        o["module_id"] for o in outcomes
        if o.get("status") == "skipped"
    }
    for adapter_id in (
        "text_enrichment", "classification_enrichment",
        "table_enrichment", "image_enrichment",
    ):
        assert adapter_id in skipped_ids, (
            f"adapter {adapter_id!r} must produce a SKIPPED outcome "
            f"when no LLM client is wired (saw {skipped_ids})"
        )
    # Reason copy must be operator-readable for the 4 legacy-
    # compatible adapters (not just empty). The skeleton modules
    # (metadata / terminology / validation) have their own skip
    # reasons that this test doesn't pin.
    legacy_ids = {
        "text_enrichment", "classification_enrichment",
        "table_enrichment", "image_enrichment",
    }
    for o in outcomes:
        if o.get("module_id") in legacy_ids:
            reason = (o.get("reason") or "").lower()
            assert "no" in reason and (
                "client" in reason or "tables" in reason or "images" in reason
            ), f"unexpected skip reason for {o['module_id']!r}: {reason!r}"


# ---- 2. Adapters run when clients are wired ------------------------


def test_activity_constructed_with_clients_runs_text_and_classification(
    workspace, artifact_registry, ctx,
):
    """Real wiring: text client + limiter present → text +
 classification + (no-table-no-image) adapters all run; table
 + image adapters still skip on input-absence."""
    text = _FakeTextClient(response={
        "category": "method_statement",
        "confidence": 0.8,
        "requirements": [{"text": "fail-safe operation"}],
    })
    limiter = _RecordingLimiter()
    activity = _activities(
        workspace, artifact_registry,
        text_client=TextLLMClientAdapter(text),
        llm_call_limiter=limiter,
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-2",
        document_id="doc-1",
        compile_result_payload=_compile_payload(),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    outcomes = result.plan_payload.get("module_outcomes") or []
    by_id = {o["module_id"]: o for o in outcomes}
    # Text + classification ran. Table + image skip (no inputs).
    assert by_id["text_enrichment"]["status"] == "run"
    assert by_id["classification_enrichment"]["status"] == "run"
    assert by_id["table_enrichment"]["status"] == "skipped"
    assert by_id["image_enrichment"]["status"] == "skipped"
    # Limiter saw at least two invocations — one per LLM-backed
    # adapter that ran. Proves the limiter reaches the adapters.
    assert len(limiter.calls) >= 2
    # Typed projection landed on the EnrichmentResult.
    assert result.plan_payload.get("classification_result") is not None
    assert result.plan_payload["classification_result"]["category"] == (
        "method_statement"
    )


def test_activity_constructed_with_vision_runs_image_module(
    workspace, artifact_registry, ctx,
):
    """Vision client + image-bytes provider produce real image
 summaries through the PerImageVisionAdapter."""
    raw_vision = _FakeVisionLLMClient(response='{"caption": "site plan"}')
    images = [
        VisionImagePayload(image_id="i-0", image_bytes=b"\x00\x01"),
        VisionImagePayload(image_id="i-1", image_bytes=b"\x02"),
    ]
    adapter = PerImageVisionAdapter(
        raw_vision, image_provider=lambda: images,
    )
    activity = _activities(
        workspace, artifact_registry, vision_client=adapter,
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-3",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=2),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    outcomes = result.plan_payload.get("module_outcomes") or []
    by_id = {o["module_id"]: o for o in outcomes}
    assert by_id["image_enrichment"]["status"] == "run"
    # Adapter looped per image — the raw client saw 2 calls.
    assert len(raw_vision.calls) == 2
    # Image summaries populated on the payload.
    image_summaries = result.plan_payload.get("image_summaries") or []
    assert len(image_summaries) == 2
    captions = {s.get("caption") for s in image_summaries}
    assert "site plan" in captions


# ---- 3. Final-report integration ----------------------------------


def test_final_report_shows_skipped_modules_when_clients_missing(
    workspace, artifact_registry, ctx,
):
    """The final ingestion report's enrichment summary must surface
 the SKIPPED adapter outcomes so operators see the deployment-
 side absence of LLM credentials in the report — not buried in
 the per-artifact endpoint."""
    activity = _activities(workspace, artifact_registry)
    enrichment = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-4",
        document_id="doc-1",
        compile_result_payload=_compile_payload(tables=1, images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    report = build_final_ingestion_report(ReportSourceInputs(
        run_id="run-4",
        document_id="doc-1",
        document_name="spec.pdf",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        started_at="2026-05-11T00:00:00+00:00",
        completed_at="2026-05-11T00:01:00+00:00",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        compile_result_summary=_compile_payload(tables=1, images=1),
        post_compile_enrich_plan=_enrich_plan_payload(),
        enrichment_result=enrichment.plan_payload,
    ))
    by_id = {o["module_id"]: o for o in
             report.enrichment_summary.module_outcomes}
    for mod_id in (
        "text_enrichment", "classification_enrichment",
        "table_enrichment", "image_enrichment",
    ):
        assert by_id[mod_id]["status"] == "skipped"


def test_final_report_shows_typed_outputs_when_clients_run(
    workspace, artifact_registry, ctx,
):
    """With fake clients wired, the final report's
 `enrichment_summary` carries the typed outputs (classification +
 retrieval hints + module outcomes)."""
    text = _FakeTextClient(response={
        "category": "method_statement", "confidence": 0.8,
        "requirements": [{"text": "fail-safe"}],
    })
    activity = _activities(
        workspace, artifact_registry,
        text_client=TextLLMClientAdapter(text),
    )
    enrichment = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-5",
        document_id="doc-1",
        compile_result_payload=_compile_payload(),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    # The enrichment_result payload now carries classification +
    # retrieval_hints.
    payload = enrichment.plan_payload
    assert payload.get("classification_result") is not None
    assert "fail-safe" in " ".join(payload.get("retrieval_hints") or [])
    # Final report surfaces the module_outcomes for the adapters
    # that ran.
    report = build_final_ingestion_report(ReportSourceInputs(
        run_id="run-5",
        document_id="doc-1",
        document_name="spec.pdf",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        started_at="2026-05-11T00:00:00+00:00",
        completed_at="2026-05-11T00:01:00+00:00",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        compile_result_summary=_compile_payload(),
        post_compile_enrich_plan=_enrich_plan_payload(),
        enrichment_result=payload,
    ))
    by_id = {o["module_id"]: o for o in
             report.enrichment_summary.module_outcomes}
    assert by_id["text_enrichment"]["status"] == "run"
    assert by_id["classification_enrichment"]["status"] == "run"


# ---- 4. Limiter reaches all LLM-backed adapters -------------------


def test_shared_limiter_reaches_text_and_classification_adapters(
    workspace, artifact_registry, ctx,
):
    """One limiter instance must wrap every adapter call — the
 operator's `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` ceiling
 applies across the whole stage."""
    text = _FakeTextClient(response={"category": "x"})
    limiter = _RecordingLimiter()
    activity = _activities(
        workspace, artifact_registry,
        text_client=TextLLMClientAdapter(text),
        llm_call_limiter=limiter,
    )
    activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-6",
        document_id="doc-1",
        compile_result_payload=_compile_payload(),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    # At least one limiter call per adapter that ran — text +
    # classification both flow through the same limiter instance.
    module_ids_seen = {
        c["metadata"].get("module_id") for c in limiter.calls
    }
    assert "text_enrichment" in module_ids_seen
    assert "classification_enrichment" in module_ids_seen


# ---- 5. Legacy-vocabulary guard -----------------------------------


def test_wiring_emits_no_legacy_vocabulary(
    workspace, artifact_registry, ctx,
):
    """The whole stage output payload must remain free of split-mode
 + pre-compile-gating wording even with adapters wired."""
    import json as _json
    text = _FakeTextClient(response={"category": "x"})
    activity = _activities(
        workspace, artifact_registry,
        text_client=TextLLMClientAdapter(text),
    )
    enrichment = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-7",
        document_id="doc-1",
        compile_result_payload=_compile_payload(),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    blob = _json.dumps(enrichment.plan_payload)
    for forbidden in (
        "split_mode", "SplitMode", "split mode",
        "pre_compile_gating", "graph gating", "index gating",
    ):
        assert forbidden not in blob
