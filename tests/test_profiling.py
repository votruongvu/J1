"""`DeterministicDocumentProfiler` regression tests.

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
    count and text extractability without parsing content deeply."""
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
    # Blank-page PDFs have no extractable text → ratio = 0.0 and
    # has_scanned_pages = True (blank == effectively scanned).
    assert profile.text_extractable_ratio == 0.0
    assert profile.has_scanned_pages is True


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


# ---- Lightweight-library policy enforcement -----------------------
#
# These tests pin the contract that the default profiler uses ONLY
# pypdf + stdlib for assessment. RAGAnything / MinerU may be invoked
# during compile, but profiling must stay sub-second + side-effect-
# free. See `j1.processing.profiling` module docstring for the full
# policy.


def test_profiler_does_not_import_raganything_or_mineru(monkeypatch):
    """A profile call MUST NOT trigger imports of RAGAnything or
    MinerU. Tripwire: monkeypatch `sys.modules` to None for both,
    then call profile — if anything in the profiler eagerly imports
    them, the import will raise and the test fails."""
    import sys
    from pathlib import Path
    monkeypatch.setitem(sys.modules, "raganything", None)
    monkeypatch.setitem(sys.modules, "mineru", None)
    profiler = DeterministicDocumentProfiler()
    src = Path(monkeypatch.__class__.__name__)  # placeholder — overwrite below
    # Use a real tmp file via tmp_path_factory pattern inline.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(b"%PDF-1.4\n%fake\n")
        src = Path(fh.name)
    try:
        profile = profiler.profile("doc-no-rag", str(src))
        # File is malformed → profile still returns; the contract
        # under test is "no RAGAnything import," not "PDF parses
        # cleanly." The presence of a profile object proves no
        # ImportError fired.
        assert profile.extension == ".pdf"
    finally:
        src.unlink(missing_ok=True)


def test_profiler_extracts_image_count_when_pdf_has_images(tmp_path):
    """The profiler counts `/XObject` entries with `/Subtype /Image`
    on sampled pages and extrapolates over the full document. A
    real-PDF count built with pypdf gives a deterministic
    integer; planner can branch on `has_images` without invoking
    the parser."""
    pypdf = pytest.importorskip("pypdf")
    src = tmp_path / "with_images.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    # Inject a tiny stream object marked as an Image XObject onto
    # the page's resource dict. We don't need actual image bytes —
    # the profiler only counts the Subtype reference.
    from pypdf.generic import (
        DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
    )
    page = writer.pages[0]
    image_stream = DecodedStreamObject()
    image_stream.set_data(b"")
    image_stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1),
        NameObject("/Height"): NumberObject(1),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
    })
    image_ref = writer._add_object(image_stream)
    resources = page[NameObject("/Resources")] if "/Resources" in page else DictionaryObject()
    xobjects = DictionaryObject({NameObject("/Im0"): image_ref})
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources
    with src.open("wb") as f:
        writer.write(f)

    profiler = DeterministicDocumentProfiler()
    profile = profiler.profile("img-doc", str(src))
    assert profile.page_count == 1
    assert profile.image_count == 1
    assert profile.has_images is True


def test_profiler_image_count_zero_when_pdf_has_no_images(tmp_path):
    """When pypdf successfully reads the PDF and finds no images,
    `image_count` is 0 (not None) — explicit "we looked, none
    present." Lets the planner skip the optional path with
    confidence rather than degrading conservatively."""
    pypdf = pytest.importorskip("pypdf")
    src = tmp_path / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with src.open("wb") as f:
        writer.write(f)

    profile = DeterministicDocumentProfiler().profile("blank", str(src))
    assert profile.image_count == 0
    assert profile.has_images is False


def test_profiler_image_count_unknown_when_pypdf_missing(
    tmp_path, monkeypatch,
):
    """Without pypdf, `image_count` is None — degrades cleanly,
    same warning surface as page_count / text_extractable_ratio."""
    import sys
    src = tmp_path / "anything.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(sys.modules, "pypdf", None)

    profile = DeterministicDocumentProfiler().profile("any", str(src))
    assert profile.image_count is None
    assert profile.has_images is None  # tri-state — we don't know


# ---- Density signals --------------------------------------------


def _write_pdf_with_text_per_page(
    path: Path, *, page_text: str, page_count: int,
) -> None:
    """Write a multi-page PDF where every page carries `page_text`
    as drawable content. Pure pypdf — no MinerU, no rendering."""
    import pypdf
    from pypdf.generic import (
        ContentStream, DictionaryObject, NameObject, NumberObject,
    )
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        page = writer.add_blank_page(width=600, height=800)
        # Embed a tiny content stream that draws `page_text` so
        # `extract_text()` actually finds chars. The exact PDF
        # operators don't matter — pypdf's text extraction sees the
        # string literal in the content stream.
        operators = [
            (b"BT", []),
            (b"/F1 12 Tf", []),
            (b"72 720 Td", []),
            (b"Tj", [pypdf.generic.TextStringObject(page_text)]),
            (b"ET", []),
        ]
        # pypdf's ContentStream constructor wants raw bytes; build
        # via an existing utility.
        # Simplest path: replace the page Contents with a stream
        # whose data is a hand-built PDF content syntax.
        content_bytes = (
            f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET\n".encode("utf-8")
        )
        from pypdf.generic import DecodedStreamObject
        content_stream = DecodedStreamObject()
        content_stream.set_data(content_bytes)
        page[NameObject("/Contents")] = writer._add_object(content_stream)
        # Minimal font resource so /F1 references resolve.
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = DictionaryObject({
            NameObject("/F1"): font_ref,
        })
        page[NameObject("/Resources")] = resources
    with path.open("wb") as f:
        writer.write(f)


def test_text_heavy_pdf_produces_high_density_signals(tmp_path):
    """A PDF where every page carries substantial text → profiler
    populates `total_text_chars` (extrapolated) and a low (0.0)
    `empty_page_ratio`. Combined with `page_count`, the planner
    derives `avg_chars_per_page` well above the LOW threshold."""
    pytest.importorskip("pypdf")
    src = tmp_path / "text_heavy.pdf"
    long_text = "Lorem ipsum dolor sit amet " * 50  # >> 100 chars/page
    _write_pdf_with_text_per_page(src, page_text=long_text, page_count=3)

    profile = DeterministicDocumentProfiler().profile("th-1", str(src))

    assert profile.total_text_chars is not None
    assert profile.total_text_chars > 0
    assert profile.empty_page_ratio == 0.0
    assert profile.has_scanned_pages is False
    avg = profile.total_text_chars / profile.page_count
    assert avg >= 100, f"text-heavy PDF should clear LOW threshold (got {avg})"


def test_blank_pdf_produces_low_density_and_deep_plan(tmp_path):
    """A blank-page PDF (no extractable text) → profiler reports
    `total_text_chars=0`, `empty_page_ratio=1.0`, and the planner
    routes the document to DEEP + OCR. Same outcome a real
    scanned-without-OCR document would produce."""
    pytest.importorskip("pypdf")
    from j1.processing.assessment import (
        Capability, CompileMode, DefaultAssessmentPlanner,
    )
    src = tmp_path / "blank.pdf"
    import pypdf
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    with src.open("wb") as f:
        writer.write(f)

    profile = DeterministicDocumentProfiler().profile("blank", str(src))
    assert profile.total_text_chars == 0
    assert profile.empty_page_ratio == 1.0
    assert profile.has_scanned_pages is True

    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.DEEP
    assert Capability.OCR in plan.required_capabilities


def test_density_low_avg_chars_triggers_deep_plan_via_density(tmp_path):
    """Edge case the binary `text_extractable_ratio` rule misses:
    a PDF where the OCR layer leaked through (page numbers / running
    headers) so each page has a TINY amount of text — enough to
    clear the 20-char binary threshold but not enough to be useful.
    The density classifier fires LOW (avg_chars_per_page < 100) and
    routes the document to DEEP + OCR despite the binary rule
    saying "text present"."""
    pytest.importorskip("pypdf")
    from j1.processing.assessment import (
        Capability, CompileMode, DefaultAssessmentPlanner,
    )
    src = tmp_path / "ocr_leak.pdf"
    # 25 chars per page — clears the binary-text-presence threshold
    # (>= 20 chars) but well below the density LOW threshold (<100).
    _write_pdf_with_text_per_page(
        src, page_text="page header text 12345", page_count=4,
    )

    profile = DeterministicDocumentProfiler().profile("leak", str(src))
    # Binary check sees text on every page — so text_extractable_ratio
    # is 1.0 and `has_scanned_pages` is False.
    assert profile.text_extractable_ratio == 1.0
    assert profile.has_scanned_pages is False
    avg = profile.total_text_chars / profile.page_count
    assert avg < 100, (
        "test design: this PDF must fall under LOW density threshold "
        f"so the planner's density branch fires (got {avg})"
    )

    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.DEEP, (
        f"density-LOW must route to DEEP even when text_extractable_ratio "
        f"is high; got mode={plan.mode}, reason={plan.reason!r}"
    )
    assert Capability.OCR in plan.required_capabilities
    assert "density" in plan.reason


def test_density_high_supports_low_complexity_standard(tmp_path):
    """A text-rich PDF with no other complexity flags → STANDARD
    mode, but `complexity=LOW` (because density says 'this should
    be cheap')."""
    pytest.importorskip("pypdf")
    from j1.processing.assessment import (
        CompileMode, Complexity, DefaultAssessmentPlanner,
    )
    src = tmp_path / "rich.pdf"
    long_text = "The quick brown fox jumps over the lazy dog. " * 20
    _write_pdf_with_text_per_page(src, page_text=long_text, page_count=3)

    profile = DeterministicDocumentProfiler().profile("rich", str(src))
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD
    assert plan.complexity == Complexity.LOW
    assert "density" in plan.reason


def test_density_signals_unknown_when_pypdf_missing(tmp_path, monkeypatch):
    """Without pypdf, the density fields stay None — same degrade-
    cleanly contract the rest of the lightweight profiler honours."""
    import sys
    src = tmp_path / "anything.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(sys.modules, "pypdf", None)

    profile = DeterministicDocumentProfiler().profile("any", str(src))
    assert profile.total_text_chars is None
    assert profile.empty_page_ratio is None


def test_density_signals_extracted_without_invoking_heavy_parser(
    tmp_path, monkeypatch,
):
    """The density extraction MUST stay inside the lightweight
    profiler — same tripwire as `test_profiler_does_not_import_*`,
    but specific to the density path. Block raganything + mineru
    imports for the duration of the call."""
    import sys
    pytest.importorskip("pypdf")
    monkeypatch.setitem(sys.modules, "raganything", None)
    monkeypatch.setitem(sys.modules, "mineru", None)
    src = tmp_path / "lite.pdf"
    long_text = "hello world. " * 50
    _write_pdf_with_text_per_page(src, page_text=long_text, page_count=2)

    profile = DeterministicDocumentProfiler().profile("lite", str(src))
    # Density signals populated → no heavy parser was needed.
    assert profile.total_text_chars is not None
    assert profile.total_text_chars > 0
    assert profile.empty_page_ratio == 0.0


def test_assessment_planner_consumes_deterministic_profile(tmp_path):
    """End-to-end micro-test: the AssessmentPlanner builds a real
    `AssessmentPlan` from a `DocumentProfile` produced by the
    deterministic profiler. Confirms the chain
    `profile -> AssessmentPlanner -> AssessmentPlan` is wired
    against the lightweight library, not the heavyweight one."""
    from j1.processing.assessment import (
        Capability, CompileMode, DefaultAssessmentPlanner,
    )
    pytest.importorskip("pypdf")
    src = tmp_path / "doc.txt"
    src.write_text("hello world\n")
    profile = DeterministicDocumentProfiler().profile("d", str(src))
    plan = DefaultAssessmentPlanner().assess(profile)
    # Plain text → fast plan, text_extraction required, no heavy
    # parser invocation.
    assert plan.mode == CompileMode.FAST
    assert Capability.TEXT_EXTRACTION in plan.required_capabilities
