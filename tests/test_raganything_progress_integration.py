"""End-to-end test: MinerU log lines → ProgressReporter via the
raganything bridge.

Verifies that when a `RAGAnythingCompileRequest` carries
`progress_reporter` + `run_id`, the bridge attaches the MinerU log
handler around the underlying `process_document_complete` call, so
log lines emitted by the (faked) raganything package land as
structured `step.progress` calls on the reporter.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from j1.processing.profiling import DocumentProfile  # noqa: F401 — keeps the dep graph stable
from j1.providers.raganything._bridge import default_compile
from j1.providers.raganything.compiler import RAGAnythingCompileRequest
from j1.providers.raganything.settings import load_raganything_settings
from j1.projects.context import ProjectContext


class _CapturingReporter:
    """Records every progress call. Mirrors the test reporter in
 test_progress_workflow_integration.py — kept inline here so
 these tests stay independently runnable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, kind, **kw):
        self.calls.append((kind, kw))
        return f"evt-{len(self.calls)}"

    def report_run_created(self, _ctx, **kw): return self._record("run.created", **kw)
    def report_document_received(self, _ctx, **kw): return self._record("document.received", **kw)
    def report_assessment_started(self, _ctx, **kw): return self._record("assessment.started", **kw)
    def report_assessment_completed(self, _ctx, **kw): return self._record("assessment.completed", **kw)
    def report_plan_generated(self, _ctx, **kw): return self._record("plan.generated", **kw)
    def report_plan_confirmed(self, _ctx, **kw): return self._record("plan.confirmed", **kw)
    def report_step_started(self, _ctx, **kw): return self._record("step.started", **kw)
    def report_step_progress(self, _ctx, **kw): return self._record("step.progress", **kw)
    def report_step_skipped(self, _ctx, **kw): return self._record("step.skipped", **kw)
    def report_step_warning(self, _ctx, **kw): return self._record("step.warning", **kw)
    def report_step_completed(self, _ctx, **kw): return self._record("step.completed", **kw)
    def report_step_failed(self, _ctx, **kw): return self._record("step.failed", **kw)
    def report_run_completed(self, _ctx, **kw): return self._record("run.completed", **kw)
    def report_run_failed(self, _ctx, **kw): return self._record("run.failed", **kw)
    def report_human_review_required(self, _ctx, **kw): return self._record("human_review.required", **kw)


def _install_fake_raganything(monkeypatch, log_lines: list[str]):
    """Install a fake `raganything` module whose
 `process_document_complete` emits the supplied log lines via
 the `mineru` logger before completing.

 Mirrors the existing fake-raganything pattern from
 test_raganything_libreoffice_preconvert.py."""

    class _FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeRAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method, **_extra,
        ):
            # `**_extra` swallows backend / vlm_url forwarded by the
            # bridge in the default vlm-http-client mode.
            mineru_logger = logging.getLogger("mineru")
            for line in log_lines:
                mineru_logger.info(line)
            outdir = Path(output_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "out.md").write_text("ok", encoding="utf-8")

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)


def _build_request(
    *,
    tmp_path,
    progress_reporter=None,
    run_id=None,
    document_filename: str = "doc-1.pdf",
    document_bytes: bytes = b"%PDF-1.4 fake bytes",
):
    """Factory: places a fake source file under the tenant/project
 raw/ area and returns a RAGAnythingCompileRequest."""
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / document_filename).write_bytes(document_bytes)

    class _FakeText:
        def generate(self, prompt): return ("ok", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0] * 8] * len(list(texts)), None)
        def dimension(self): return 8
        def max_tokens(self): return 512

    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(tmp_path / "rag-workdir"),
    })
    return RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id=Path(document_filename).stem,
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
        progress_reporter=progress_reporter,
        run_id=run_id,
    )


# ---- Tests ---------------------------------------------------


def test_mineru_log_line_during_compile_routes_to_reporter(
    tmp_path, monkeypatch,
):
    """A `[MinerU] Layout Preparation: 50% | 22/44` line emitted
 DURING `process_document_complete` becomes a structured
 `step.progress` call on the reporter — without any
 workflow-side log parsing or stdout capture."""
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    _install_fake_raganything(monkeypatch, log_lines=[
        "[MinerU] Layout Preparation: 50% | 22/44",
        "[MinerU] get transformers predictor cost: 50.12s",
    ])

    reporter = _CapturingReporter()
    request = _build_request(
        tmp_path=tmp_path,
        progress_reporter=reporter,
        run_id="run-42",
    )
    result = default_compile(request)

    # The compile itself succeeded.
    from j1.processing.results import ResultStatus
    assert result.status == ResultStatus.SUCCEEDED

    # Progress events landed on the reporter.
    progress_calls = [c for c in reporter.calls if c[0] == "step.progress"]
    completed_calls = [c for c in reporter.calls if c[0] == "step.completed"]

    assert len(progress_calls) >= 1
    assert progress_calls[0][1]["run_id"] == "run-42"
    assert progress_calls[0][1]["stage"] == "COMPILE"
    assert progress_calls[0][1]["step"] == "LAYOUT_PREPARATION"
    assert progress_calls[0][1]["progress_percent"] == 50
    assert progress_calls[0][1]["engine"] == "MinerU"

    # The "predictor cost" line maps to step.completed for MODEL_LOAD.
    assert any(c[1].get("step") == "MODEL_LOAD" for c in completed_calls)


def test_mineru_log_handler_not_attached_when_run_id_missing(
    tmp_path, monkeypatch,
):
    """The handler is a no-op when `run_id` is empty, even with a
 reporter present. The reporter's per-event correlation requires
 a stable run_id; without one the handler installation is skipped
 entirely so unrelated log output isn't captured."""
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    _install_fake_raganything(monkeypatch, log_lines=[
        "[MinerU] Layout Preparation: 80% | 35/44",
    ])

    reporter = _CapturingReporter()
    request = _build_request(
        tmp_path=tmp_path,
        progress_reporter=reporter,
        run_id=None,
    )
    default_compile(request)

    # No progress events should have been routed.
    assert reporter.calls == []


def test_mineru_log_handler_no_op_without_reporter(tmp_path, monkeypatch):
    """Backwards-compat: existing callers don't pass a reporter and
 the bridge runs unchanged. Verify by capturing whether ANY
 handler was attached to the mineru logger after the call."""
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    _install_fake_raganything(monkeypatch, log_lines=[])

    request = _build_request(tmp_path=tmp_path, progress_reporter=None)
    handlers_before = list(logging.getLogger("mineru").handlers)
    default_compile(request)
    handlers_after = list(logging.getLogger("mineru").handlers)

    # No handler leaked.
    assert handlers_after == handlers_before
