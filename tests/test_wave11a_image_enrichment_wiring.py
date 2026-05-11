"""Wave 11A — vision/image runtime wiring + hardening tests.

Pins:
  1. `WorkspaceImageBytesProvider` resolves `compile.image`
     artifacts into `VisionImagePayload` records.
  2. Missing image artifacts → empty payloads + clear warnings.
  3. The activity constructs `PerImageVisionAdapter` per-run.
  4. Image enrichment runs end-to-end when fake vision client +
     real workspace image bytes are present.
  5. `ImageSummary` carries `ProvenanceLink` back to the
     `compile.image` artifact id.
  6. Raw compile artifacts are not mutated.
  7. Limiter still wraps the vision analysis call.
  8. Final ingestion report reflects image module outcomes.
  9. Required vs optional enrichment failure mapping is stable.
 10. No legacy gating vocabulary anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.payloads import (
    ProjectScope,
    RunEnrichmentStageInput,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.processing.compile_result import (
    DetectedImage,
    NormalizedCompileResult,
)
from j1.processing.enrich_assessment import (
    EnrichRecommendation,
    PostCompileEnrichPlan,
)
from j1.processing.enrichment_clients import (
    PerImageVisionAdapter,
    VisionImagePayload,
    WorkspaceImageBytesProvider,
)
from j1.processing.final_ingestion_report import (
    ReportSourceInputs,
    build_final_ingestion_report,
)
from j1.processing.final_status import (
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
)
from j1.processing.service import ProcessingService
from j1.workspace.layout import WorkspaceArea


# ---- Fakes ---------------------------------------------------------


class _FakeUsage:
    def __init__(self, model="fake", input_tokens=10, output_tokens=20,
                 provider="fake-vendor"):
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeVisionLLMClient:
    """Production-shape vision client: per-image bytes input, text
    response. The activity wraps this in `PerImageVisionAdapter` at
    `run_enrichment_stage` time."""

    def __init__(self, response_template='{"caption": "image %d"}'):
        self._template = response_template
        self.calls: list[dict] = []

    def analyze_image(self, image, *, prompt, media_type=None, metadata=None):
        n = len(self.calls)
        self.calls.append({
            "bytes_len": len(image), "media_type": media_type,
        })
        return (self._template % n, _FakeUsage())


class _RecordingLimiter:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, callable_, *args, metadata=None):
        self.calls.append({"metadata": dict(metadata or {})})
        return callable_(*args)


# ---- Helpers -------------------------------------------------------


def _write_image_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    artifact_id: str,
    image_bytes: bytes,
    document_id: str = "doc-1",
    suffix: str = ".png",
) -> ArtifactRecord:
    """Persist a `compile.image` artifact to the workspace +
    registry so `WorkspaceImageBytesProvider` can load it back."""
    filename = f"{artifact_id}{suffix}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(image_bytes)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="compile.image",
        location=f"compiled/{filename}",
        content_hash=f"hash-{artifact_id}",
        byte_size=len(image_bytes),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata={"document_id": document_id},
    )
    artifact_registry.add(record)
    return record


def _make_activity(workspace, artifact_registry, **kwargs):
    """Minimal `ProcessingActivities` constructor — same composition
    path the deploy wiring uses, but stripped to enrichment-only."""
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.cost.recorder import DefaultCostRecorder
    from j1.cost.sink import JsonlCostSink

    audit = DefaultAuditRecorder(JsonlAuditSink(workspace))
    cost = DefaultCostRecorder(JsonlCostSink(workspace))
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifact_registry,
        audit=audit, cost=cost,
    )

    class _NullSources:
        def get(self, c, doc_id):
            raise LookupError(doc_id)

    return ProcessingActivities(
        processing=processing, sources=_NullSources(),
        artifacts=artifact_registry, **kwargs,
    )


def _compile_payload(*, images: int = 0):
    return NormalizedCompileResult(
        document_id="doc-1",
        status="succeeded",
        raw_artifact_refs=("raw-1",),
        chunks_count=5,
        extracted_text_chars=5_000,
        detected_images=tuple(
            DetectedImage(image_id=f"detected-{i}", page=1)
            for i in range(images)
        ),
    ).to_payload()


def _enrich_plan_payload(*, require_success: bool = False):
    return PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
        require_enrichment_success=require_success,
    ).to_payload()


# ---- 1. WorkspaceImageBytesProvider behaviour ---------------------


def test_provider_loads_compile_image_artifacts(
    workspace, artifact_registry, ctx,
):
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-1", image_bytes=b"\x89PNG\r\n",
        suffix=".png",
    )
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-2", image_bytes=b"\xff\xd8\xff",
        suffix=".jpg",
    )
    provider = WorkspaceImageBytesProvider(
        artifact_registry=artifact_registry,
        workspace=workspace, ctx=ctx, document_id="doc-1",
    )
    payloads = list(provider())
    assert len(payloads) == 2
    by_id = {p.image_id: p for p in payloads}
    assert by_id["img-1"].image_bytes == b"\x89PNG\r\n"
    assert by_id["img-1"].media_type == "image/png"
    assert by_id["img-1"].source_artifact_id == "img-1"
    assert by_id["img-2"].media_type == "image/jpeg"


def test_provider_returns_empty_when_no_image_artifacts_match(
    workspace, artifact_registry, ctx,
):
    provider = WorkspaceImageBytesProvider(
        artifact_registry=artifact_registry,
        workspace=workspace, ctx=ctx, document_id="doc-1",
    )
    result = provider.load_all()
    assert result.payloads == ()
    assert result.warnings == ()


def test_provider_emits_warning_when_image_bytes_unreadable(
    workspace, artifact_registry, ctx,
):
    """Artifact registered but underlying file missing — provider
    must surface a clear warning so the operator sees the miss."""
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="img-missing", project=ctx, kind="compile.image",
        location="compiled/missing.png",
        content_hash="hash-missing", byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"document_id": "doc-1"},
    ))
    provider = WorkspaceImageBytesProvider(
        artifact_registry=artifact_registry,
        workspace=workspace, ctx=ctx, document_id="doc-1",
    )
    result = provider.load_all()
    assert result.payloads == ()
    assert len(result.warnings) == 1
    warn = result.warnings[0].lower()
    assert "img-missing" in warn
    assert "not loadable" in warn


def test_provider_filters_by_document_id(
    workspace, artifact_registry, ctx,
):
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-doc-1", image_bytes=b"x", document_id="doc-1",
    )
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-other-doc", image_bytes=b"y", document_id="doc-2",
    )
    provider = WorkspaceImageBytesProvider(
        artifact_registry=artifact_registry,
        workspace=workspace, ctx=ctx, document_id="doc-1",
    )
    payloads = list(provider())
    ids = {p.image_id for p in payloads}
    assert ids == {"img-doc-1"}


def test_provider_caches_results_across_calls(
    workspace, artifact_registry, ctx,
):
    """Adapter may invoke provider multiple times in one stage; the
    cache keeps the second call from re-listing the registry."""
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-1", image_bytes=b"\x89PNG\r\n",
    )
    provider = WorkspaceImageBytesProvider(
        artifact_registry=artifact_registry,
        workspace=workspace, ctx=ctx, document_id="doc-1",
    )
    first = provider.load_all()
    second = provider.load_all()
    assert first is second  # cached identity


# ---- 2. Per-run adapter construction ------------------------------


def test_activity_constructs_per_run_image_adapter(
    workspace, artifact_registry, ctx,
):
    """The activity must wrap the RAW vision client in a fresh
    `PerImageVisionAdapter` with a workspace-aware provider for
    the current run — not use an empty bootstrap-time adapter."""
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-a", image_bytes=b"\x89PNG\r\n",
    )
    raw_vision = _FakeVisionLLMClient(
        response_template='{"caption": "page %d figure"}',
    )
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=raw_vision,
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-1",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    payload = result.plan_payload
    by_id = {o["module_id"]: o for o in payload["module_outcomes"]}
    assert by_id["image_enrichment"]["status"] == "run"
    # The raw vision client saw exactly one per-image call.
    assert len(raw_vision.calls) == 1
    assert raw_vision.calls[0]["bytes_len"] > 0


def test_activity_image_module_skips_when_no_artifacts_loadable(
    workspace, artifact_registry, ctx,
):
    """`compile_result.detected_images` says images exist but no
    matching `compile.image` artifacts are persisted → image module
    runs but the adapter returns no payloads. The provider's
    "no images" outcome plus the module's PARTIAL/skip logic must
    yield a clear non-success outcome."""
    raw_vision = _FakeVisionLLMClient()
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=raw_vision,
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-2",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=2),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    payload = result.plan_payload
    by_id = {o["module_id"]: o for o in payload["module_outcomes"]}
    # Image module ran (had a client) but produced no parseable
    # summaries (adapter returned empty `images: []`).
    outcome = by_id["image_enrichment"]
    assert outcome["status"] == "partial"
    # No LLM calls happened — provider returned no payloads.
    assert raw_vision.calls == []


# ---- 3. End-to-end with real workspace image bytes ----------------


def test_image_enrichment_produces_image_summaries_with_provenance(
    workspace, artifact_registry, ctx,
):
    for i in range(2):
        _write_image_artifact(
            workspace, artifact_registry, ctx,
            artifact_id=f"img-{i}",
            image_bytes=b"\x89PNG\r\n",
        )
    raw_vision = _FakeVisionLLMClient(
        response_template='{"caption": "figure %d", "role": "diagram"}',
    )
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=raw_vision,
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-3",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=2),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    payload = result.plan_payload
    summaries = payload["image_summaries"]
    assert len(summaries) == 2
    # Each ImageSummary carries a ProvenanceLink to the source
    # compile artifact id.
    for summary in summaries:
        assert summary["caption"]
        prov = summary["provenance"]
        assert prov["source_artifact_id"] == "raw-1"
        assert prov["source_kind"] == "compile"
        assert prov["relation"] == "extracted_from"


def test_provider_warnings_flow_to_image_module_outcome(
    workspace, artifact_registry, ctx,
):
    """Detected images + unreadable bytes → image outcome carries
    operator-readable warnings."""
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="img-missing", project=ctx, kind="compile.image",
        location="compiled/does-not-exist.png",
        content_hash="hash-x", byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"document_id": "doc-1"},
    ))
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=_FakeVisionLLMClient(),
    )
    result = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-4",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    payload = result.plan_payload
    by_id = {o["module_id"]: o for o in payload["module_outcomes"]}
    image_outcome = by_id["image_enrichment"]
    # The provider warned about the missing file — the activity
    # spliced that onto the outcome's warnings list.
    joined = " ".join(image_outcome["warnings"])
    assert "img-missing" in joined
    assert "not loadable" in joined.lower()


# ---- 4. Raw compile artifacts are not mutated ----------------------


def test_image_enrichment_does_not_mutate_raw_compile_artifacts(
    workspace, artifact_registry, ctx,
):
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-pre", image_bytes=b"\x89PNG\r\n",
    )
    # Capture the artifact's hash + bytes BEFORE enrichment.
    before = artifact_registry.get(ctx, "img-pre")
    raw_bytes_before = (
        workspace.area(ctx, WorkspaceArea.COMPILED) / "img-pre.png"
    ).read_bytes()

    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=_FakeVisionLLMClient(),
    )
    activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-5",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))

    after = artifact_registry.get(ctx, "img-pre")
    raw_bytes_after = (
        workspace.area(ctx, WorkspaceArea.COMPILED) / "img-pre.png"
    ).read_bytes()
    # Bytes + registry metadata both intact.
    assert raw_bytes_after == raw_bytes_before
    assert after.content_hash == before.content_hash
    assert after.byte_size == before.byte_size


# ---- 5. Limiter wraps the vision path -----------------------------


def test_shared_limiter_wraps_vision_call(
    workspace, artifact_registry, ctx,
):
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-1", image_bytes=b"\x89PNG\r\n",
    )
    limiter = _RecordingLimiter()
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=_FakeVisionLLMClient(),
        enrichment_llm_call_limiter=limiter,
    )
    activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-6",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    image_calls = [
        c for c in limiter.calls
        if c["metadata"].get("module_id") == "image_enrichment"
    ]
    # One acquisition wraps the batch analyze call (per-image
    # bounding is documented as deferred — Wave 11B).
    assert len(image_calls) == 1


# ---- 6. Final-report integration ----------------------------------


def test_final_report_records_image_module_outcome_and_summaries(
    workspace, artifact_registry, ctx,
):
    _write_image_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="img-a", image_bytes=b"\x89PNG\r\n",
    )
    activity = _make_activity(
        workspace, artifact_registry,
        enrichment_vision_client=_FakeVisionLLMClient(
            response_template='{"caption": "site plan %d"}',
        ),
    )
    enrichment = activity.run_enrichment_stage(RunEnrichmentStageInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="run-7",
        document_id="doc-1",
        compile_result_payload=_compile_payload(images=1),
        post_compile_enrich_plan_payload=_enrich_plan_payload(),
    ))
    report = build_final_ingestion_report(ReportSourceInputs(
        run_id="run-7",
        document_id="doc-1",
        document_name="spec.pdf",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        started_at="2026-05-11T00:00:00+00:00",
        completed_at="2026-05-11T00:01:00+00:00",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        compile_result_summary=_compile_payload(images=1),
        post_compile_enrich_plan=_enrich_plan_payload(),
        enrichment_result=enrichment.plan_payload,
    ))
    by_id = {o["module_id"]: o for o in
             report.enrichment_summary.module_outcomes}
    assert by_id["image_enrichment"]["status"] == "run"


# ---- 7. Required vs optional enrichment failure mapping -----------


def test_required_enrichment_failure_maps_to_failed_enrichment_required(
    workspace, artifact_registry, ctx,
):
    """Wave-11A hardening — pin the failure mapping that's already
    in place. With `require_enrichment_success=True` and a failed
    enrichment outcome, the final-status projection lands at
    `failed_enrichment_required` (not `completed_with_warnings`)."""
    from j1.processing.final_status import project_final_status

    projection = project_final_status(
        framework_final_status="failed",
        failure_code="ENRICHMENT_REQUIRED",
        enrichment_status="failed",
        enrichment_required=True,
    )
    assert projection.status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED


def test_optional_enrichment_failure_maps_to_completed_with_warnings():
    """With `require_enrichment_success=False`, the same failed
    outcome lands at `completed_with_enrichment_warnings` so the
    operator sees the issue without the run being marked FAILED."""
    from j1.processing.final_status import project_final_status

    projection = project_final_status(
        framework_final_status="completed",
        failure_code=None,
        enrichment_status="failed",
        enrichment_required=False,
    )
    assert projection.status == (
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS
    )


# ---- 8. Legacy-vocabulary guards ----------------------------------


def test_enrichment_clients_source_still_has_no_legacy_vocabulary():
    import inspect
    from j1.processing import enrichment_clients
    src = inspect.getsource(enrichment_clients)
    for forbidden in (
        "split_mode", "SplitMode", "split mode",
        "insert_content",
        "pre_compile_gating", "graph gating", "index gating",
    ):
        assert forbidden not in src


def test_wave11a_provider_has_no_hardcoded_civil_engineering_terms():
    """The provider is generic; no domain-specific terms should
    leak in (the operator-readable warning copy must stay neutral)."""
    import inspect
    from j1.processing.enrichment_clients import WorkspaceImageBytesProvider
    src = inspect.getsource(WorkspaceImageBytesProvider)
    for term in (
        "RFI", "BOQ", "civil_engineering", "method statement",
        "structural drawing",
    ):
        assert term not in src
