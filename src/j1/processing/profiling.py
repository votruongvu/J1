"""Document profile + profiler interface for adaptive ingestion planning.

A `DocumentProfile` captures cheap, deterministic signals about an
input document — extension, MIME type, size, page count, basic
content hints. The legacy planning path and the
[`AssessmentPlanner`](./assessment.py) both consume this to decide
which pipeline stages to run and how to configure compile.

Profilers are split from planners deliberately: a profiler reports
*what the document looks like*; a planner decides *what to do about it*.
This makes both halves cheaply testable and lets a deployment swap
either independently (e.g. a heavyweight LLM-assisted profiler with
the default deterministic planner).

## Lightweight-library policy

The default `DeterministicDocumentProfiler` uses ONLY:

 * **`pypdf`** (pure-Python) for PDF page count + text-extractability
 sampling + per-page `/XObject` image counting.
 * **`mimetypes`** (stdlib) for MIME guessing from extension.
 * **`os.stat`** for file size.

It MUST NOT call:

 * `RAGAnything.process_document_complete` — full ingestion,
 minutes per document.
 * `RAGAnything.parse_document` — parser-only, but still loads
 MinerU + transformers (gigabytes of model weights).
 * Any other heavyweight parser (MinerU CLI, Unstructured, etc.).

RAGAnything / MinerU are COMPILE EXECUTORS, not profilers. The
compile stage may delegate to them; assessment must not. This rule
is what keeps profiling sub-second and lets the planner decide
"is this worth ~minutes of GPU/LLM time" before the cost is paid.

## Adding richer profilers later

A future `MinerUProfilerAdapter` (or any other vendor) can be
added behind the `DocumentProfiler` Protocol. Drop it next to
`DeterministicDocumentProfiler`; deployments switch via wiring.
Keep the contract:

 * Sub-second on typical inputs.
 * No I/O beyond reading the source file.
 * No model downloads, no network calls.
 * Failures degrade to a `DocumentProfile` with `warnings=("...",)`
 rather than raising (file-not-found is the only exception).
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DocumentProfile:
    """What we know about a document before kicking off the pipeline.

 Every field except `document_id` and `extension` is allowed to be
 `None`/`unknown` — the deterministic profiler only fills in what
 it can reliably observe without invoking the full parser. The
 planner treats `None` as "I don't know" and respects policy when
 deciding whether to default conservatively or aggressively.

 Field reference:
 * `extension` — lowercased file suffix WITH the leading dot
 (e.g. ".pdf", ".txt"). Always populated; this is the primary
 cheap signal.
 * `mime_type` — best-effort MIME guess from `mimetypes` (stdlib).
 `None` when no extension match is registered.
 * `file_size_bytes` — `os.stat` size. Useful for cost prediction.
 * `page_count` — for PDFs, populated when `pypdf` is available
 and the document is well-formed; `None` for non-PDFs or when
 we can't read it cheaply.
 * `text_extractable_ratio` — 0..1, fraction of pages with
 embedded text (vs. pure scans). Set to 1.0 for plain-text
 extensions; `None` when unknown.
 * `has_images` / `has_tables` / `has_scanned_pages` — three-state
 bool: True / False / None. `None` means the deterministic
 profiler couldn't tell; the planner may consult an LLM
 fallback or default conservatively.
 * `estimated_tokens` — rough upper bound for cost estimation.
 * `language` — ISO 639-1 code if detected; `None` otherwise.
 * `parser_confidence` — 0..1, how confident we are the document
 will parse cleanly. Mostly populated by deeper profilers; the
 deterministic one leaves it `None`.
 * `warnings` — operational hints the planner should surface
 (e.g. "extension and MIME disagree", "file > 100MB"). Free-
 form strings; never document content.
 """

    document_id: str
    extension: str
    mime_type: str | None = None
    file_size_bytes: int | None = None
    page_count: int | None = None
    text_extractable_ratio: float | None = None
    has_images: bool | None = None
    has_tables: bool | None = None
    has_scanned_pages: bool | None = None
    estimated_tokens: int | None = None
    language: str | None = None
    parser_confidence: float | None = None
    warnings: tuple[str, ...] = ()
    # ---- Manifest signals (post-parse) -----------------------------
    # Populated by the workflow after compile completes, by merging
    # the parser's `content_stats` into the deterministic profile.
    # All optional — pre-parse the deterministic profiler can't see
    # any of these. Quality scores are 0..1, deterministic, derived
    # from the counts so `_compute_quality_scores` can re-derive
    # them anywhere without re-reading the parsed content.
    image_count: int | None = None
    table_count: int | None = None
    equation_count: int | None = None
    text_block_count: int | None = None
    total_text_chars: int | None = None
    empty_page_ratio: float | None = None
    parse_quality_score: float | None = None
    text_sufficiency_score: float | None = None
    layout_complexity_score: float | None = None
    # Per-image triage decisions surfaced by the parser. Each entry
    # is a small dict (image_id, decision, role, score, reason, page,
    # caption, size_bytes); the planner reads it and exposes the same
    # list as `IngestPlan.vision_decisions`. None = parser doesn't
    # surface per-image data; the planner falls back to the coarse
    # `requires_vision` doc-level decision.
    images: tuple[dict[str, object], ...] | None = None


class DocumentProfiler(Protocol):
    """Profiler interface: hands back a `DocumentProfile` for a path.

 Implementations MUST be cheap (sub-second) and side-effect free.
 They MUST NOT invoke the full document parser, run an LLM, or
 download anything — the profiler runs before the planner has
 decided whether the document is worth processing at all.

 Failures: a profiler MAY raise on truly unrecoverable input
 (e.g. file doesn't exist) but SHOULD prefer returning a profile
 with `warnings=("...",)` so the planner can fall back to a
 safe-default mode (typically MULTIMODAL_FULL)."""

    kind: str

    def profile(self, document_id: str, source_path: str) -> DocumentProfile: ...


# Plain-text extensions where we can confidently set
# `text_extractable_ratio=1.0` without inspecting bytes — they're
# either text-by-definition or so common that "treat as text" is the
# right default.
_PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv",
})

# Above this size we emit a warning so the planner knows to bias
# toward streaming-friendly modes. Not a hard limit — the planner
# ultimately decides.
_LARGE_FILE_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB


@dataclass(frozen=True)
class DeterministicDocumentProfiler:
    """Cheap, side-effect-free profiler.

 Inspects:
 * file extension via `Path.suffix`
 * MIME type via stdlib `mimetypes.guess_type`
 * file size via `os.stat`
 * for `.pdf`: page count via `pypdf` (already a transitive dep
 of the raganything provider; gracefully no-ops if absent so
 deployments without raganything still get a useful profile)

 Sets `text_extractable_ratio=1.0` for plain-text extensions; leaves
 it `None` everywhere else. Higher-tier profilers (LLM-assisted,
 deeper PDF inspection) are encouraged to extend this class rather
 than reimplement.

 Profile failures: file-not-found raises; everything else (PDF
 parse errors, MIME ambiguity, etc.) is downgraded to a warning
 so the planner can fall back to a conservative mode."""

    kind: str = "deterministic"

    def profile(
        self, document_id: str, source_path: str
    ) -> DocumentProfile:
        path = Path(source_path)
        if not path.exists():
            # File-not-found is not a profile-recoverable error —
            # the planner can't compensate for a missing source.
            raise FileNotFoundError(
                f"document {document_id!r} not found at {source_path}"
            )

        warnings: list[str] = []

        extension = path.suffix.lower()
        mime_type = mimetypes.guess_type(path.name)[0]
        try:
            file_size_bytes = os.stat(path).st_size
        except OSError as exc:
            warnings.append(f"stat() failed: {exc}")
            file_size_bytes = None

        if (
            file_size_bytes is not None
            and file_size_bytes > _LARGE_FILE_THRESHOLD_BYTES
        ):
            warnings.append(
                f"file size {file_size_bytes:,} bytes exceeds "
                f"{_LARGE_FILE_THRESHOLD_BYTES:,} threshold"
            )

        # Plain-text shortcut.
        if extension in _PLAIN_TEXT_EXTENSIONS:
            return DocumentProfile(
                document_id=document_id,
                extension=extension,
                mime_type=mime_type,
                file_size_bytes=file_size_bytes,
                page_count=1,                     # treat as a single chunk
                text_extractable_ratio=1.0,
                has_images=False,
                has_tables=False,
                has_scanned_pages=False,
                warnings=tuple(warnings),
            )

        # PDF inspection — best-effort; absence of pypdf or a malformed
        # file just leaves page_count unknown.
        page_count: int | None = None
        text_extractable_ratio: float | None = None
        has_scanned_pages: bool | None = None
        image_count: int | None = None
        has_images: bool | None = None
        total_text_chars: int | None = None
        empty_page_ratio: float | None = None
        if extension == ".pdf":
            (
                page_count, text_extractable_ratio, has_scanned_pages,
                image_count, total_text_chars, empty_page_ratio,
                pdf_warnings,
            ) = _safe_pdf_profile(path)
            warnings.extend(pdf_warnings)
            if image_count is not None:
                # Tri-state: derive `has_images` only when the count
                # is actually known. Zero images = explicit False so
                # the planner sees "we looked, none present" rather
                # than "we don't know."
                has_images = image_count > 0

        return DocumentProfile(
            document_id=document_id,
            extension=extension,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            text_extractable_ratio=text_extractable_ratio,
            has_images=has_images,
            has_tables=None,  # not cheaply detectable; needs layout analysis
            has_scanned_pages=has_scanned_pages,
            image_count=image_count,
            # Density signals (pre-compile, sample-extrapolated).
            # Planner uses these as soft bias toward fast vs. deep
            # mode without committing to OCR before parse_method is
            # picked. See `assessment.py:_pick_mode_density_bias`.
            total_text_chars=total_text_chars,
            empty_page_ratio=empty_page_ratio,
            warnings=tuple(warnings),
        )


def _safe_pdf_profile(
    path: Path,
    *,
    sample_pages: int = 5,
) -> tuple[
    int | None,        # page_count
    float | None,      # text_extractable_ratio
    bool | None,       # has_scanned_pages
    int | None,        # image_count (extrapolated)
    int | None,        # total_text_chars (extrapolated)
    float | None,      # empty_page_ratio (sampled)
    list[str],         # warnings
]:
    """Read page count + text density signals + image count via
 `pypdf`. Best-effort.

 Samples up to `sample_pages` pages and reports:
 * `text_extractable_ratio` — fraction of sampled pages with
 ≥ 20 non-whitespace chars (binary per-page text presence).
 * `has_scanned_pages` — derived from the ratio.
 * `image_count` — approximate document-wide count of embedded
 images, extrapolated from the sample.
 * `total_text_chars` — approximate document-wide non-whitespace
 char count, extrapolated from the sample. Combined with
 `page_count`, the planner derives `avg_chars_per_page` to
 bias toward fast vs. deep modes.
 * `empty_page_ratio` — fraction of sampled pages with NO
 extractable text (< 20 chars). Distinct from
 `text_extractable_ratio` (which counts pages WITH text):
 `empty + with_text` may not sum to 1 exactly when the
 sample size differs from `total`.

 Returns:
 (page_count, text_extractable_ratio, has_scanned_pages,
 image_count, total_text_chars, empty_page_ratio, warnings)

 We deliberately stop at text + image counting — no image pixel
 sampling, no table detection, no OCR. Those are a deeper
 profiler's job. This gives the planner the cheapest useful
 density signal."""
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:
        warnings.append(
            "pypdf not installed — page_count, text_extractable_ratio, "
            "image_count, total_text_chars, and empty_page_ratio "
            "unavailable; planner will fall back to mode-default "
            "heuristics"
        )
        return None, None, None, None, None, None, warnings
    try:
        reader = PdfReader(str(path))
        total = len(reader.pages)
        if total == 0:
            return 0, None, None, 0, 0, None, warnings
        n = min(sample_pages, total)
        pages_with_text = 0
        empty_pages = 0
        sampled_images = 0
        sampled_chars = 0
        for i in range(n):
            page = reader.pages[i]
            page_text = (page.extract_text() or "").strip()
            page_chars = len(page_text)
            sampled_chars += page_chars
            if page_chars >= 20:
                pages_with_text += 1
            else:
                empty_pages += 1
            sampled_images += _count_images_on_page(page)
        ratio = round(pages_with_text / n, 2)
        empty_ratio = round(empty_pages / n, 2)
        if ratio >= 0.8:
            has_scanned_pages: bool | None = False
        elif ratio < 0.1:
            has_scanned_pages = True
        else:
            has_scanned_pages = None  # mixed — planner decides conservatively
        # Extrapolate sampled counts over the full document. Rough
        # but enough for planner gating ("is this text-rich?").
        # Operators chasing exact counts should run a deeper
        # profiler.
        scale = total / n if n > 0 else 1
        image_count = int(round(sampled_images * scale))
        total_text_chars = int(round(sampled_chars * scale))
        return (
            total, ratio, has_scanned_pages,
            image_count, total_text_chars, empty_ratio,
            warnings,
        )
    except Exception as exc:  # noqa: BLE001 — any pypdf error is recoverable
        warnings.append(f"pypdf could not read {path.name!r}: {exc!s}")
        return None, None, None, None, None, None, warnings


def _count_images_on_page(page) -> int:
    """Count `/XObject` entries on `page` whose `/Subtype` is
 `/Image`. Defensive — pypdf's resource dict can be missing,
 malformed, or use indirect references; on any inspection error
 we return 0 rather than raise (the caller's outer try/except
 catches harder failures already).

 The check tolerates pypdf's two-shape resource dict: indirect
 object that needs `.get_object` resolution OR a plain dict.
 """
    try:
        resources = page.get("/Resources")
        if resources is None:
            return 0
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return 0
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        count = 0
        for ref in xobjects.values():
            obj = ref.get_object() if hasattr(ref, "get_object") else ref
            if obj.get("/Subtype") == "/Image":
                count += 1
        return count
    except Exception:  # noqa: BLE001 — defensive
        return 0
