"""Phase B regression tests: `DeterministicDocumentProfiler`.

The profiler must be cheap, side-effect free, and degrade gracefully
when optional dependencies (pypdf) are absent or when the input is
malformed. These tests pin its public contract so future changes
can't accidentally make profiling expensive or fail-loud."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from j1.processing.profiling import (
    DeterministicDocumentProfiler,
    DocumentProfile,
)


@pytest.fixture
def profiler() -> DeterministicDocumentProfiler:
    return DeterministicDocumentProfiler()


# ---- Plain-text fast path -----------------------------------------


def test_plain_text_profile_marks_extractable_and_no_modalities(
    tmp_path, profiler,
):
    src = tmp_path / "doc-1.txt"
    src.write_text("hello world\n", encoding="utf-8")

    profile = profiler.profile("doc-1", str(src))

    assert isinstance(profile, DocumentProfile)
    assert profile.extension == ".txt"
    assert profile.mime_type == "text/plain"
    assert profile.file_size_bytes == src.stat().st_size
    # Plain text → confident: 1 page, fully extractable, no modalities.
    assert profile.page_count == 1
    assert profile.text_extractable_ratio == 1.0
    assert profile.has_images is False
    assert profile.has_tables is False
    assert profile.has_scanned_pages is False
    assert profile.warnings == ()


def test_markdown_extension_takes_plain_text_fast_path(tmp_path, profiler):
    src = tmp_path / "notes.md"
    src.write_text("# heading", encoding="utf-8")

    profile = profiler.profile("notes", str(src))

    assert profile.extension == ".md"
    assert profile.text_extractable_ratio == 1.0


# ---- Unknown extension: leaves modality flags None -----------------


def test_unknown_extension_leaves_modality_signals_unknown(
    tmp_path, profiler,
):
    """For non-text, non-PDF inputs the profiler can't cheaply observe
    modality signals. It MUST leave them `None` so the planner knows
    to fall back to safe defaults / LLM hints, not pretend the
    document is empty."""
    src = tmp_path / "doc.docx"
    src.write_bytes(b"PK\x03\x04 fake docx bytes")

    profile = profiler.profile("doc-x", str(src))

    assert profile.extension == ".docx"
    assert profile.text_extractable_ratio is None
    assert profile.has_images is None
    assert profile.has_tables is None
    assert profile.has_scanned_pages is None


# ---- PDF page count: best-effort, non-fatal -----------------------


def test_pdf_page_count_uses_pypdf_when_available(tmp_path, profiler):
    """If `pypdf` is importable (which it is in this repo via
    raganything's transitive deps), the profiler reads the page
    count without parsing content."""
    pypdf = pytest.importorskip("pypdf")
    src = tmp_path / "report.pdf"
    # Build a tiny 2-page PDF with pypdf so the test is deterministic.
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    with src.open("wb") as f:
        writer.write(f)

    profile = profiler.profile("rpt-1", str(src))

    assert profile.extension == ".pdf"
    assert profile.page_count == 2
    # PDFs don't get the plain-text shortcut — modality flags stay None.
    assert profile.text_extractable_ratio is None


def test_pdf_page_count_warning_when_pypdf_missing(
    tmp_path, profiler, monkeypatch,
):
    """If `pypdf` isn't installed, the profiler MUST NOT raise — it
    leaves `page_count=None` and emits a `warnings` entry so the
    planner can still decide."""
    src = tmp_path / "anything.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    # Simulate pypdf missing — block the import for the duration of
    # this test only.
    monkeypatch.setitem(sys.modules, "pypdf", None)

    profile = profiler.profile("any", str(src))

    assert profile.page_count is None
    assert any("pypdf" in w for w in profile.warnings)


def test_pdf_page_count_warning_on_malformed_file(tmp_path, profiler):
    """Malformed PDFs must NOT raise — pypdf throws, we catch, and
    surface a single warning so the planner falls back to a
    conservative default."""
    src = tmp_path / "broken.pdf"
    src.write_bytes(b"this is definitely not a pdf")

    profile = profiler.profile("broken-1", str(src))

    assert profile.page_count is None
    assert any("pypdf could not read" in w for w in profile.warnings)


# ---- Failure mode: missing file -----------------------------------


def test_missing_source_raises_file_not_found(tmp_path, profiler):
    """File-not-found is the one error the profiler is allowed to
    raise — the planner can't compensate for absent input."""
    with pytest.raises(FileNotFoundError):
        profiler.profile("ghost", str(tmp_path / "does-not-exist.pdf"))


# ---- Large-file warning -------------------------------------------


def test_large_file_emits_size_warning(tmp_path, profiler):
    """Files above the 100 MB threshold get a warning so the planner
    can pick a streaming-friendly mode. Use a sparse file so the
    test isn't slowed by an actual 250 MB write — `truncate` reserves
    the size in metadata without writing any data blocks."""
    src = tmp_path / "big.docx"
    target_size = 250 * 1024 * 1024
    with src.open("wb") as f:
        f.truncate(target_size)

    profile = profiler.profile("big-1", str(src))

    assert profile.file_size_bytes == target_size
    assert any("file size" in w for w in profile.warnings)


# ---- No side effects ----------------------------------------------


def test_profiler_does_not_modify_source(tmp_path, profiler):
    """Profiling MUST be read-only — the planner runs the profiler on
    every document and we don't want side effects on the workspace."""
    src = tmp_path / "doc.txt"
    body = b"unchanged"
    src.write_bytes(body)
    mtime_before = src.stat().st_mtime
    size_before = src.stat().st_size

    profiler.profile("d-1", str(src))

    assert src.read_bytes() == body
    assert src.stat().st_size == size_before
    assert src.stat().st_mtime == mtime_before
