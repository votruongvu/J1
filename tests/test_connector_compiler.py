import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.connectors.compiler import (
    ARTIFACT_KIND_CONCEPT,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_SUMMARY,
    AdapterRequest,
    AdapterResponse,
    CallableCompilerAdapter,
    CompilerConfig,
    ExternalKnowledgeCompiler,
    SubprocessCompilerAdapter,
)
from j1.cost.breakdown import CostBreakdown
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import (
    CompilerConfigError,
    CompilerExecutionError,
)
from j1.jobs.status import ProcessingStatus
from j1.processing.status import ResultStatus


# ---- Helpers -----------------------------------------------------------


def _document(ctx) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="paper.txt",
        stored_filename="doc-1.txt",
        mime_type="text/plain",
        file_size=5,
        checksum="sha256:doc-1",
        status=ProcessingStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _stage_source(workspace, ctx, content: bytes = b"hello") -> DocumentRecord:
    record = _document(ctx)
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / record.stored_filename).write_bytes(content)
    return record


def _read_audit(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- CompilerConfig ----------------------------------------------------


def test_config_defaults():
    cfg = CompilerConfig()
    assert cfg.enabled is True
    assert cfg.adapter == "callable"
    assert cfg.command == ()
    assert cfg.timeout_seconds == 300.0
    assert cfg.output_mapping == {}


def test_effective_output_mapping_uses_default_when_empty():
    cfg = CompilerConfig()
    mapping = cfg.effective_output_mapping()
    assert mapping["summary.md"] == ARTIFACT_KIND_SUMMARY
    assert mapping["concepts.json"] == ARTIFACT_KIND_CONCEPT
    assert mapping["log.txt"] == ARTIFACT_KIND_LOG


def test_effective_output_mapping_overrides_default():
    custom = {"out.bin": "compiled_index"}
    cfg = CompilerConfig(output_mapping=custom)
    assert cfg.effective_output_mapping() == custom


# ---- CallableCompilerAdapter -------------------------------------------


def test_callable_adapter_invokes_function():
    captured: dict[str, AdapterRequest] = {}

    def fn(req: AdapterRequest) -> AdapterResponse:
        captured["req"] = req
        return AdapterResponse(log="done")

    adapter = CallableCompilerAdapter(fn)
    req = AdapterRequest(
        workspace_dir=Path("/tmp"),
        input_file=Path("/tmp/x"),
        config=CompilerConfig(),
    )
    response = adapter.execute(req)
    assert response.log == "done"
    assert captured["req"] is req


# ---- SubprocessCompilerAdapter -----------------------------------------


def test_subprocess_adapter_runs_command(tmp_path):
    workspace_dir = tmp_path / "work"
    workspace_dir.mkdir()
    input_file = tmp_path / "input.txt"
    input_file.write_text("hello")

    config = CompilerConfig(
        adapter="subprocess",
        command=(
            sys.executable,
            "-c",
            (
                "import sys, pathlib; "
                "out = pathlib.Path(sys.argv[1]); "
                "(out / 'summary.md').write_text('hi'); "
                "print('done')"
            ),
            "{outdir}",
        ),
        timeout_seconds=10.0,
    )
    adapter = SubprocessCompilerAdapter()
    response = adapter.execute(
        AdapterRequest(
            workspace_dir=workspace_dir,
            input_file=input_file,
            config=config,
            metadata={"document_id": "doc-1"},
        )
    )
    assert (workspace_dir / "summary.md").read_text() == "hi"
    assert "done" in response.log
    assert any(p.name == "summary.md" for p in response.output_files)


def test_subprocess_adapter_requires_command(tmp_path):
    adapter = SubprocessCompilerAdapter()
    with pytest.raises(CompilerConfigError):
        adapter.execute(
            AdapterRequest(
                workspace_dir=tmp_path,
                input_file=tmp_path / "x",
                config=CompilerConfig(adapter="subprocess"),
            )
        )


def test_subprocess_adapter_raises_on_nonzero_exit(tmp_path):
    workspace_dir = tmp_path / "work"
    workspace_dir.mkdir()
    config = CompilerConfig(
        adapter="subprocess",
        command=(sys.executable, "-c", "import sys; sys.exit(2)"),
        timeout_seconds=10.0,
    )
    adapter = SubprocessCompilerAdapter()
    with pytest.raises(CompilerExecutionError) as exc:
        adapter.execute(
            AdapterRequest(
                workspace_dir=workspace_dir,
                input_file=tmp_path / "x",
                config=config,
            )
        )
    assert "exited with code 2" in str(exc.value)


def test_subprocess_adapter_raises_on_timeout(tmp_path):
    workspace_dir = tmp_path / "work"
    workspace_dir.mkdir()
    config = CompilerConfig(
        adapter="subprocess",
        command=(sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_seconds=0.5,
    )
    adapter = SubprocessCompilerAdapter()
    with pytest.raises(CompilerExecutionError) as exc:
        adapter.execute(
            AdapterRequest(
                workspace_dir=workspace_dir,
                input_file=tmp_path / "x",
                config=config,
            )
        )
    assert "timed out" in str(exc.value)


def test_subprocess_adapter_raises_on_missing_binary(tmp_path):
    workspace_dir = tmp_path / "work"
    workspace_dir.mkdir()
    config = CompilerConfig(
        adapter="subprocess",
        command=("/nonexistent/binary",),
        timeout_seconds=10.0,
    )
    adapter = SubprocessCompilerAdapter()
    with pytest.raises(CompilerExecutionError) as exc:
        adapter.execute(
            AdapterRequest(
                workspace_dir=workspace_dir,
                input_file=tmp_path / "x",
                config=config,
            )
        )
    assert "not found" in str(exc.value)


# ---- ExternalKnowledgeCompiler -----------------------------------------


def _make_compiler(workspace, registry, audit_recorder=None, *, fn=None, config=None):
    def default_fn(req: AdapterRequest) -> AdapterResponse:
        (req.workspace_dir / "summary.md").write_text("# summary")
        (req.workspace_dir / "concepts.json").write_text('{"e":1}')
        return AdapterResponse(
            output_files=sorted(p for p in req.workspace_dir.iterdir() if p.is_file()),
            log="ok",
        )

    return ExternalKnowledgeCompiler(
        config=config or CompilerConfig(enabled=True),
        adapter=CallableCompilerAdapter(fn or default_fn),
        workspace=workspace,
        sources=registry,
        audit=audit_recorder,
    )


def test_compiler_kind_default():
    compiler = ExternalKnowledgeCompiler(
        config=CompilerConfig(),
        adapter=CallableCompilerAdapter(lambda r: AdapterResponse()),
        workspace=None,  # not used in this attribute check
        sources=None,
    )
    assert compiler.kind == "external_knowledge_compiler"


def test_compiler_disabled_returns_skipped(workspace, registry, ctx):
    compiler = _make_compiler(
        workspace, registry, config=CompilerConfig(enabled=False)
    )
    result = compiler.compile(ctx, "doc-1")
    assert result.status is ResultStatus.SKIPPED
    assert "disabled" in (result.message or "")


def test_compiler_returns_failed_when_document_missing(workspace, registry, ctx):
    compiler = _make_compiler(workspace, registry)
    result = compiler.compile(ctx, "missing-doc")
    assert result.status is ResultStatus.FAILED
    assert "missing-doc" in (result.error or "")


def test_compiler_returns_failed_when_source_file_missing(
    workspace, registry, ctx
):
    registry.add(_document(ctx))  # registered but file not staged
    compiler = _make_compiler(workspace, registry)
    result = compiler.compile(ctx, "doc-1")
    assert result.status is ResultStatus.FAILED
    assert "source file missing" in (result.error or "")


def test_compiler_returns_drafts_for_mapped_outputs(workspace, registry, ctx):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))
    compiler = _make_compiler(workspace, registry)
    result = compiler.compile(ctx, "doc-1")
    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.drafts) == 2
    kinds = {d.kind for d in result.drafts}
    assert kinds == {ARTIFACT_KIND_SUMMARY, ARTIFACT_KIND_CONCEPT}
    for draft in result.drafts:
        assert draft.source_document_ids == ["doc-1"]


def test_compiler_skips_unmapped_outputs(workspace, registry, ctx):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))

    def fn(req):
        (req.workspace_dir / "summary.md").write_text("ok")
        (req.workspace_dir / "garbage.bin").write_bytes(b"x")  # unmapped
        return AdapterResponse(
            output_files=sorted(p for p in req.workspace_dir.iterdir() if p.is_file()),
        )

    compiler = _make_compiler(workspace, registry, fn=fn)
    result = compiler.compile(ctx, "doc-1")
    assert {d.kind for d in result.drafts} == {ARTIFACT_KIND_SUMMARY}


def test_compiler_propagates_cost_events(workspace, registry, ctx):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))

    breakdown = CostBreakdown(
        vendor="anthropic",
        model="claude",
        unit_kind="input_tokens",
        units=42,
        amount=Decimal("0.0123"),
    )

    def fn(req):
        (req.workspace_dir / "summary.md").write_text("ok")
        return AdapterResponse(
            output_files=[req.workspace_dir / "summary.md"],
            cost_breakdowns=[breakdown],
        )

    compiler = _make_compiler(workspace, registry, fn=fn)
    result = compiler.compile(ctx, "doc-1")
    assert result.cost_events == [breakdown]


def test_compiler_returns_failed_on_adapter_exception(workspace, registry, ctx):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))

    def fn(req):
        raise CompilerExecutionError("boom")

    compiler = _make_compiler(workspace, registry, fn=fn)
    result = compiler.compile(ctx, "doc-1")
    assert result.status is ResultStatus.FAILED
    assert result.error == "boom"
    assert result.message == "CompilerExecutionError"


def test_compiler_writes_audit_invoked_and_completed(
    workspace, registry, audit_recorder, ctx
):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))
    compiler = _make_compiler(workspace, registry, audit_recorder=audit_recorder)
    compiler.compile(ctx, "doc-1")

    actions = [e["action"] for e in _read_audit(workspace, ctx)]
    assert "j1.connector.compiler.invoked" in actions
    assert "j1.connector.compiler.completed" in actions


def test_compiler_writes_audit_failed_on_adapter_exception(
    workspace, registry, audit_recorder, ctx
):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))

    def fn(req):
        raise RuntimeError("kaboom")

    compiler = _make_compiler(workspace, registry, audit_recorder=audit_recorder, fn=fn)
    compiler.compile(ctx, "doc-1")

    events = _read_audit(workspace, ctx)
    failed = [e for e in events if e["action"] == "j1.connector.compiler.failed"]
    assert failed
    assert failed[0]["payload"]["error"] == "kaboom"
    assert failed[0]["payload"]["error_type"] == "RuntimeError"


def test_compiler_writes_audit_skipped_when_disabled(
    workspace, registry, audit_recorder, ctx
):
    compiler = _make_compiler(
        workspace,
        registry,
        audit_recorder=audit_recorder,
        config=CompilerConfig(enabled=False),
    )
    compiler.compile(ctx, "doc-1")
    events = _read_audit(workspace, ctx)
    assert any(e["action"] == "j1.connector.compiler.skipped" for e in events)


def test_compiler_no_audit_when_recorder_not_provided(workspace, registry, ctx):
    _stage_source(workspace, ctx)
    registry.add(_document(ctx))
    compiler = _make_compiler(workspace, registry, audit_recorder=None)
    result = compiler.compile(ctx, "doc-1")
    assert result.status is ResultStatus.SUCCEEDED
    assert _read_audit(workspace, ctx) == []


def test_compiler_can_be_used_as_processor_via_processing_service(
    workspace, registry, artifact_registry, processing_service, ctx
):
    """Connector implements KnowledgeCompiler protocol; ProcessingService can drive it."""
    _stage_source(workspace, ctx)
    document = _document(ctx)
    registry.add(document)
    compiler = _make_compiler(workspace, registry)

    result = processing_service.compile(ctx, compiler, document)
    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.artifacts) == 2
    kinds = {r.kind for r in result.artifacts}
    assert kinds == {ARTIFACT_KIND_SUMMARY, ARTIFACT_KIND_CONCEPT}
    # ArtifactRegistry now has the records.
    listed_kinds = {r.kind for r in artifact_registry.list_artifacts(ctx)}
    assert listed_kinds == {ARTIFACT_KIND_SUMMARY, ARTIFACT_KIND_CONCEPT}
