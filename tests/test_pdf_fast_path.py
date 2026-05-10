"""Tests for the PDF text-layer fast path that bypasses MinerU.

The fast path activates when >= 80 % of sampled PDF pages have
extractable text.  It inserts the extracted text directly into
LightRAG via `lightrag.ainsert`, skipping
`RAGAnything.process_document_complete` (which starts MinerU and runs
minutes of transformer inference for no benefit on plain text PDFs).

Coverage:
  * `_is_text_extractable_pdf` correctly classifies text vs scan PDFs.
  * `DeterministicDocumentProfiler` now populates
    `text_extractable_ratio` and `has_scanned_pages` for PDFs.
  * `default_compile` uses fast path (no MinerU) for text-layer PDFs.
  * `default_compile` falls through to MinerU for scanned PDFs.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from j1.processing.profiling import DeterministicDocumentProfiler
from j1.providers.raganything._bridge import _is_text_extractable_pdf


# ---- PDF fixtures --------------------------------------------------


def _write_text_pdf(path: Path, n_pages: int = 4) -> None:
    """Write a text-layer PDF using reportlab."""
    pytest.importorskip("reportlab", reason="reportlab required for PDF fixtures")
    from reportlab.pdfgen import canvas as rl_canvas  # type: ignore[import-untyped]

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    for i in range(n_pages):
        c.drawString(
            72, 700,
            f"Page {i + 1}. The quick brown fox jumps over the lazy dog. "
            "Sample text for automated tests.",
        )
        c.showPage()
    c.save()
    buf.seek(0)
    path.write_bytes(buf.read())


def _write_blank_pdf(path: Path, n_pages: int = 4) -> None:
    """Write a PDF with blank pages (no text) - simulates scanned document."""
    pypdf = pytest.importorskip("pypdf", reason="pypdf required for PDF fixtures")
    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    path.write_bytes(buf.read())


# ---- _is_text_extractable_pdf -------------------------------------


def test_text_pdf_is_detected_as_extractable(tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    pdf = tmp_path / "text.pdf"
    _write_text_pdf(pdf, n_pages=4)

    assert _is_text_extractable_pdf(pdf) is True


def test_blank_scan_pdf_is_not_extractable(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "scan.pdf"
    _write_blank_pdf(pdf, n_pages=4)

    assert _is_text_extractable_pdf(pdf) is False


def test_non_pdf_is_not_extractable(tmp_path):
    """Non-PDF paths always return False regardless of content."""
    docx = tmp_path / "word.docx"
    docx.write_bytes(b"PK...")
    assert _is_text_extractable_pdf(docx) is False


def test_missing_pypdf_returns_false(tmp_path, monkeypatch):
    """If pypdf is not importable, fall through to MinerU conservatively."""
    pdf = tmp_path / "any.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(sys.modules, "pypdf", None)

    assert _is_text_extractable_pdf(pdf) is False


def test_corrupt_pdf_returns_false(tmp_path):
    """Corrupt PDFs fall through to MinerU - no exception raised."""
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"this is definitely not a PDF")

    assert _is_text_extractable_pdf(pdf) is False


def test_threshold_respected(tmp_path):
    """At exactly the threshold (80 %) returns True; 1.01 always False."""
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")

    pdf = tmp_path / "text.pdf"
    _write_text_pdf(pdf, n_pages=4)

    assert _is_text_extractable_pdf(pdf, threshold=0.8) is True
    # Threshold 1.01 - no real PDF can satisfy this.
    assert _is_text_extractable_pdf(pdf, threshold=1.01) is False


# ---- DeterministicDocumentProfiler PDF profiling -------------------


def test_profiler_text_pdf_sets_text_ratio_and_not_scanned(tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    profiler = DeterministicDocumentProfiler()
    pdf = tmp_path / "report.pdf"
    _write_text_pdf(pdf, n_pages=4)

    profile = profiler.profile("doc-text", str(pdf))

    assert profile.extension == ".pdf"
    assert profile.page_count == 4
    assert profile.text_extractable_ratio is not None
    assert profile.text_extractable_ratio >= 0.8
    assert profile.has_scanned_pages is False


def test_profiler_scan_pdf_sets_low_ratio_and_scanned(tmp_path):
    pytest.importorskip("pypdf")
    profiler = DeterministicDocumentProfiler()
    pdf = tmp_path / "scanned.pdf"
    _write_blank_pdf(pdf, n_pages=4)

    profile = profiler.profile("doc-scan", str(pdf))

    assert profile.page_count == 4
    assert profile.text_extractable_ratio is not None
    assert profile.text_extractable_ratio < 0.1
    assert profile.has_scanned_pages is True


def test_profiler_pdf_no_pypdf_leaves_fields_none(tmp_path, monkeypatch):
    """When pypdf is absent, both new fields must remain None (graceful)."""
    pdf = tmp_path / "any.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(sys.modules, "pypdf", None)

    profiler = DeterministicDocumentProfiler()
    profile = profiler.profile("any", str(pdf))

    assert profile.text_extractable_ratio is None
    assert profile.has_scanned_pages is None
    assert any("pypdf" in w for w in profile.warnings)


# ---- default_compile routing: fast path vs MinerU ------------------


def _make_fake_rag(*, ainsert_mock: AsyncMock) -> SimpleNamespace:
    lightrag = SimpleNamespace(ainsert=ainsert_mock)
    return SimpleNamespace(
        lightrag=lightrag,
        _ensure_lightrag_initialized=AsyncMock(return_value={"success": True}),
        process_document_complete=AsyncMock(),
    )


def _place_source(pdf: Path, workspace: Path) -> tuple[str, Path]:
    """Copy pdf into the workspace raw dir; return (document_id, dest)."""
    raw_dir = workspace / "tenants" / "t1" / "projects" / "p1" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / pdf.name
    dest.write_bytes(pdf.read_bytes())
    return dest.stem, dest


def _make_request(workspace: Path, document_id: str) -> SimpleNamespace:
    from j1.providers.raganything.settings import RAGAnythingSettings

    settings = RAGAnythingSettings(
        workdir=str(workspace / "rag"),
        pdf_convert_extensions=(),
    )
    return SimpleNamespace(
        ctx=SimpleNamespace(tenant_id="t1", project_id="p1"),
        document_id=document_id,
        settings=settings,
        text_client=None,
        vision_client=None,
        embedding_client=None,
        progress_reporter=None,
        run_id=None,
    )


def _run_with_fake_rag(request: SimpleNamespace, rag: SimpleNamespace, workspace: Path):
    """Call default_compile with _prepare_compile patched to inject our fake rag."""
    from j1.providers.raganything import _bridge as bridge

    raw = (
        workspace / "tenants"
        / request.ctx.tenant_id / "projects" / request.ctx.project_id / "raw"
    )
    candidates = list(raw.glob(f"{request.document_id}*"))
    assert candidates, f"no source file found for {request.document_id}"
    source_path = candidates[0]
    output_dir = workspace / "rag" / "outputs" / request.document_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # `_prepare_compile` returns 4-tuple now: (rag, output_dir, source_path,
    # dropped_config_overrides). Empty list = no plan overrides dropped.
    with patch.object(bridge, "_prepare_compile", return_value=(rag, output_dir, source_path, [])):
        return bridge.default_compile(request)


def test_text_pdf_compile_uses_fast_path_not_mineru(tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")

    pdf = tmp_path / "text_doc.pdf"
    _write_text_pdf(pdf, n_pages=4)
    document_id, _ = _place_source(pdf, tmp_path)

    ainsert = AsyncMock()
    rag = _make_fake_rag(ainsert_mock=ainsert)
    request = _make_request(tmp_path, document_id)

    result = _run_with_fake_rag(request, rag, tmp_path)

    assert result.status.value == "succeeded"
    # Fast path: ainsert called, process_document_complete NOT called.
    ainsert.assert_awaited_once()
    rag.process_document_complete.assert_not_awaited()


def test_scan_pdf_compile_uses_mineru_not_fast_path(tmp_path):
    pytest.importorskip("pypdf")

    pdf = tmp_path / "scan_doc.pdf"
    _write_blank_pdf(pdf, n_pages=4)
    document_id, _ = _place_source(pdf, tmp_path)

    ainsert = AsyncMock()
    rag = _make_fake_rag(ainsert_mock=ainsert)
    request = _make_request(tmp_path, document_id)

    result = _run_with_fake_rag(request, rag, tmp_path)

    assert result.status.value == "succeeded"
    # Full MinerU path: process_document_complete called, ainsert NOT called.
    rag.process_document_complete.assert_awaited_once()
    ainsert.assert_not_awaited()
