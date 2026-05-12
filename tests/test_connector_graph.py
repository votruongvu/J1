import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.connectors.graph import (
    ARTIFACT_KIND_GRAPH_HTML,
    ARTIFACT_KIND_GRAPH_JSON,
    ARTIFACT_KIND_GRAPH_REPORT,
    DEFAULT_GRAPH_OUTPUT_MAPPING,
    CallableGraphAdapter,
    ExternalGraphBuilder,
    GraphAdapterRequest,
    GraphAdapterResponse,
    GraphConfig,
    SubprocessGraphAdapter,
)
from j1.cost.breakdown import CostBreakdown
from j1.errors.exceptions import (
    GraphConfigError,
    GraphExecutionError,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.status import ResultStatus
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader
from j1.workspace.layout import WorkspaceArea


# ---- Helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str,
    kind: str = "compiled.text",
    content: bytes = b"hello",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    artifact_registry.add(record)
    return record


def _read_audit(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def default_profile() -> Profile:
    return ProfileLoader().load(DEFAULT_PROFILE_ID)


# ---- Config ------------------------------------------------------------


def test_config_defaults():
    cfg = GraphConfig()
    assert cfg.enabled is True
    assert cfg.adapter == "callable"
    assert cfg.command == ()
    assert cfg.timeout_seconds == 300.0
    assert cfg.corpus_include == ()
    assert cfg.cache_enabled is True


def test_default_output_mapping_exposes_five_kinds():
    assert set(DEFAULT_GRAPH_OUTPUT_MAPPING.values()) == {
        "graph_json",
        "graph_html",
        "graph_report",
        "graph_cache",
        "graph_metadata",
    }


def test_effective_mapping_uses_default_when_empty():
    cfg = GraphConfig()
    assert cfg.effective_output_mapping() == DEFAULT_GRAPH_OUTPUT_MAPPING


def test_effective_mapping_overrides_default():
    custom = {"out.json": "graph_json"}
    cfg = GraphConfig(output_mapping=custom)
    assert cfg.effective_output_mapping() == custom


# ---- CallableGraphAdapter ---------------------------------------------


def test_callable_adapter_invokes_function(tmp_path):
    captured: dict[str, GraphAdapterRequest] = {}

    def fn(req):
        captured["req"] = req
        return GraphAdapterResponse(log="ok")

    adapter = CallableGraphAdapter(fn)
    req = GraphAdapterRequest(
        workspace_dir=tmp_path,
        corpus_dir=tmp_path,
        artifacts=[],
        config=GraphConfig(),
    )
    response = adapter.execute(req)
    assert response.log == "ok"
    assert captured["req"] is req


# ---- SubprocessGraphAdapter -------------------------------------------


def test_subprocess_adapter_runs_command(tmp_path):
    workspace_dir = tmp_path / "out"
    workspace_dir.mkdir()
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "a.txt").write_text("content")

    config = GraphConfig(
        adapter="subprocess",
        command=(
            sys.executable,
            "-c",
            (
                "import sys, pathlib; "
                "out = pathlib.Path(sys.argv[1]); "
                "(out / 'graph.json').write_text('{\"nodes\":[]}'); "
                "print('built')"
            ),
            "{outdir}",
        ),
        timeout_seconds=10.0,
    )
    adapter = SubprocessGraphAdapter()
    response = adapter.execute(
        GraphAdapterRequest(
            workspace_dir=workspace_dir,
            corpus_dir=corpus_dir,
            artifacts=[],
            config=config,
        )
    )
    assert (workspace_dir / "graph.json").read_text() == '{"nodes":[]}'
    assert "built" in response.log


def test_subprocess_adapter_requires_command(tmp_path):
    adapter = SubprocessGraphAdapter()
    with pytest.raises(GraphConfigError):
        adapter.execute(
            GraphAdapterRequest(
                workspace_dir=tmp_path,
                corpus_dir=tmp_path,
                artifacts=[],
                config=GraphConfig(adapter="subprocess"),
            )
        )


def test_subprocess_adapter_missing_binary(tmp_path):
    adapter = SubprocessGraphAdapter()
    with pytest.raises(GraphExecutionError) as exc:
        adapter.execute(
            GraphAdapterRequest(
                workspace_dir=tmp_path,
                corpus_dir=tmp_path,
                artifacts=[],
                config=GraphConfig(
                    adapter="subprocess",
                    command=("/nonexistent/binary",),
                    timeout_seconds=5.0,
                ),
            )
        )
    assert "not found" in str(exc.value)


def test_subprocess_adapter_nonzero_exit(tmp_path):
    adapter = SubprocessGraphAdapter()
    with pytest.raises(GraphExecutionError) as exc:
        adapter.execute(
            GraphAdapterRequest(
                workspace_dir=tmp_path,
                corpus_dir=tmp_path,
                artifacts=[],
                config=GraphConfig(
                    adapter="subprocess",
                    command=(sys.executable, "-c", "import sys; sys.exit(3)"),
                    timeout_seconds=10.0,
                ),
            )
        )
    assert "exited with code 3" in str(exc.value)


# ---- ExternalGraphBuilder ---------------------------------------------


def _make_builder(
    workspace,
    artifact_registry,
    profile,
    *,
    audit=None,
    fn=None,
    config=None,
):
    def default_fn(req):
        (req.workspace_dir / "graph.json").write_text(
            '{"nodes": [], "edges": []}'
        )
        (req.workspace_dir / "report.md").write_text("# Graph Report\n")
        return GraphAdapterResponse(
            output_files=sorted(p for p in req.workspace_dir.iterdir() if p.is_file()),
            log="ok",
        )

    return ExternalGraphBuilder(
        config=config or GraphConfig(enabled=True),
        adapter=CallableGraphAdapter(fn or default_fn),
        workspace=workspace,
        artifacts=artifact_registry,
        profile=profile,
        audit=audit,
    )


def test_builder_kind_default(default_profile):
    builder = ExternalGraphBuilder(
        config=GraphConfig(),
        adapter=CallableGraphAdapter(lambda r: GraphAdapterResponse()),
        workspace=None,
        artifacts=None,
        profile=default_profile,
    )
    assert builder.kind == "external_graph_builder"


def test_builder_disabled_returns_skipped(workspace, artifact_registry, default_profile, ctx):
    builder = _make_builder(
        workspace, artifact_registry, default_profile,
        config=GraphConfig(enabled=False),
    )
    result = builder.build(ctx, ["a"])
    assert result.status is ResultStatus.SKIPPED


def test_builder_returns_failed_on_missing_artifact(
    workspace, artifact_registry, default_profile, ctx
):
    builder = _make_builder(workspace, artifact_registry, default_profile)
    result = builder.build(ctx, ["missing"])
    assert result.status is ResultStatus.FAILED


def test_builder_returns_failed_when_content_file_missing(
    workspace, artifact_registry, default_profile, ctx
):
    record = ArtifactRecord(
        artifact_id="art-no-file",
        project=ctx,
        kind="compiled.text",
        location="compiled/art-no-file.txt",
        content_hash="sha256:x",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    artifact_registry.add(record)
    builder = _make_builder(workspace, artifact_registry, default_profile)
    result = builder.build(ctx, ["art-no-file"])
    assert result.status is ResultStatus.FAILED
    assert "missing" in (result.error or "")


def test_builder_produces_drafts_for_mapped_outputs(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="a-2", kind="enriched.requirements",
        area=WorkspaceArea.ENRICHED,
    )
    builder = _make_builder(workspace, artifact_registry, default_profile)
    result = builder.build(ctx, ["a-1", "a-2"])
    assert result.status is ResultStatus.SUCCEEDED
    kinds = {d.kind for d in result.drafts}
    assert kinds == {ARTIFACT_KIND_GRAPH_JSON, ARTIFACT_KIND_GRAPH_REPORT}
    for draft in result.drafts:
        assert draft.source_artifact_ids == ["a-1", "a-2"]


def test_builder_skips_unmapped_outputs(workspace, artifact_registry, default_profile, ctx):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    def fn(req):
        (req.workspace_dir / "graph.json").write_text("{}")
        (req.workspace_dir / "garbage.bin").write_bytes(b"x")
        return GraphAdapterResponse(
            output_files=sorted(p for p in req.workspace_dir.iterdir() if p.is_file()),
        )

    builder = _make_builder(workspace, artifact_registry, default_profile, fn=fn)
    result = builder.build(ctx, ["a-1"])
    assert {d.kind for d in result.drafts} == {ARTIFACT_KIND_GRAPH_JSON}


def test_builder_corpus_include_filters_artifacts(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="c-1", kind="compiled.text")
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="e-1", kind="enriched.requirements",
        area=WorkspaceArea.ENRICHED,
    )

    seen_ids: list[str] = []

    def fn(req):
        seen_ids.extend([a.artifact_id for a in req.artifacts])
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"]
        )

    builder = _make_builder(
        workspace,
        artifact_registry,
        default_profile,
        fn=fn,
        config=GraphConfig(corpus_include=("enriched.requirements",)),
    )
    builder.build(ctx, ["c-1", "e-1"])
    assert seen_ids == ["e-1"]  # only enriched.requirements passed through


def test_builder_propagates_cost_events(workspace, artifact_registry, default_profile, ctx):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    breakdown = CostBreakdown(
        vendor="anthropic",
        model="m",
        unit_kind="input_tokens",
        units=10,
        amount=Decimal("0.001"),
    )

    def fn(req):
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"],
            cost_breakdowns=[breakdown],
        )

    builder = _make_builder(workspace, artifact_registry, default_profile, fn=fn)
    result = builder.build(ctx, ["a-1"])
    assert result.cost_events == [breakdown]


def test_builder_returns_failed_on_adapter_exception(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    def fn(req):
        raise GraphExecutionError("boom")

    builder = _make_builder(workspace, artifact_registry, default_profile, fn=fn)
    result = builder.build(ctx, ["a-1"])
    assert result.status is ResultStatus.FAILED
    assert result.error == "boom"
    assert result.message == "GraphExecutionError"


def test_builder_writes_audit_invoked_and_completed(
    workspace, artifact_registry, audit_recorder, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    builder = _make_builder(
        workspace, artifact_registry, default_profile, audit=audit_recorder
    )
    builder.build(ctx, ["a-1"])
    actions = [e["action"] for e in _read_audit(workspace, ctx)]
    assert "j1.connector.graph.invoked" in actions
    assert "j1.connector.graph.completed" in actions


def test_builder_writes_audit_failed_on_adapter_exception(
    workspace, artifact_registry, audit_recorder, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    def fn(req):
        raise RuntimeError("kaboom")

    builder = _make_builder(
        workspace, artifact_registry, default_profile, audit=audit_recorder, fn=fn
    )
    builder.build(ctx, ["a-1"])
    failed = [
        e for e in _read_audit(workspace, ctx)
        if e["action"] == "j1.connector.graph.failed"
    ]
    assert failed
    assert failed[0]["payload"]["error_type"] == "RuntimeError"


def test_builder_writes_audit_skipped_when_disabled(
    workspace, artifact_registry, audit_recorder, default_profile, ctx
):
    builder = _make_builder(
        workspace, artifact_registry, default_profile,
        audit=audit_recorder, config=GraphConfig(enabled=False),
    )
    builder.build(ctx, ["a-1"])
    assert any(
        e["action"] == "j1.connector.graph.skipped"
        for e in _read_audit(workspace, ctx)
    )


def test_builder_passes_cache_dir_when_enabled(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    seen: dict[str, Path | None] = {}

    def fn(req):
        seen["cache_dir"] = req.cache_dir
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"]
        )

    builder = _make_builder(
        workspace, artifact_registry, default_profile, fn=fn,
        config=GraphConfig(enabled=True, cache_enabled=True),
    )
    builder.build(ctx, ["a-1"])
    assert seen["cache_dir"] is not None
    assert seen["cache_dir"].is_dir()
    # Should be inside the project's runtime area.
    assert workspace.runtime(ctx) in seen["cache_dir"].parents or seen["cache_dir"].parent == workspace.runtime(ctx)


def test_builder_omits_cache_dir_when_disabled(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    seen: dict[str, Path | None] = {}

    def fn(req):
        seen["cache_dir"] = req.cache_dir
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"]
        )

    builder = _make_builder(
        workspace, artifact_registry, default_profile, fn=fn,
        config=GraphConfig(enabled=True, cache_enabled=False),
    )
    builder.build(ctx, ["a-1"])
    assert seen["cache_dir"] is None


def test_builder_passes_taxonomy_from_profile(
    workspace, artifact_registry, ctx
):
    profile = Profile(
        profile_id="custom",
        metadata={},
        graph_taxonomy={"node_types": ["concept"], "edge_types": ["relates_to"]},
    )
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")

    seen_taxonomy = {}

    def fn(req):
        seen_taxonomy.update(req.taxonomy)
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"]
        )

    builder = _make_builder(workspace, artifact_registry, profile, fn=fn)
    builder.build(ctx, ["a-1"])
    assert seen_taxonomy == {"node_types": ["concept"], "edge_types": ["relates_to"]}


def test_builder_corpus_writes_one_file_per_artifact(
    workspace, artifact_registry, default_profile, ctx
):
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="a-1", content=b"alpha"
    )
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="a-2", content=b"beta"
    )

    seen_contents: list[bytes] = []

    def fn(req):
        for path in sorted(req.corpus_dir.iterdir()):
            seen_contents.append(path.read_bytes())
        (req.workspace_dir / "graph.json").write_text("{}")
        return GraphAdapterResponse(
            output_files=[req.workspace_dir / "graph.json"]
        )

    builder = _make_builder(workspace, artifact_registry, default_profile, fn=fn)
    builder.build(ctx, ["a-1", "a-2"])
    assert set(seen_contents) == {b"alpha", b"beta"}


# ---- Integration: build_graph through ProcessingService ---------------


def test_builder_can_be_used_via_processing_service(
    workspace, artifact_registry, processing_service, default_profile, ctx
):
    """End-to-end: the external graph builder produces a graph_json
    draft that the legacy ``ProcessingService.build_graph`` path
    successfully registers.

    Must pass a ``correlation_id`` — graph_json carries a strict
    lineage guard at the legacy path now (the latest validation
    report flagged 7 graph_json rows with ``run_id=None``).
    Without the correlation_id this test would fail with
    ``LineageError`` — see
    ``test_processing_service_lineage_guard.py`` for that
    regression.
    """
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    builder = _make_builder(workspace, artifact_registry, default_profile)
    result = processing_service.build_graph(
        ctx, builder, ["a-1"], correlation_id="run-test-1",
    )
    assert result.status is ResultStatus.SUCCEEDED
    kinds = {r.kind for r in result.artifacts}
    assert ARTIFACT_KIND_GRAPH_JSON in kinds
    assert ARTIFACT_KIND_GRAPH_REPORT in kinds
    for record in result.artifacts:
        assert record.location.startswith("graph/")
        if record.kind == ARTIFACT_KIND_GRAPH_JSON:
            # graph_json must carry run_id — the production failure
            # mode operators hit was graph_json without lineage.
            assert record.metadata.get("run_id") == "run-test-1"
