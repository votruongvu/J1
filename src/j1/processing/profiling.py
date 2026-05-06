"""Document profile + profiler interface for adaptive ingestion planning.

A `DocumentProfile` captures cheap, deterministic signals about an
input document — extension, MIME type, size, page count, basic
content hints. The `IngestPlanner` consumes this to decide which
pipeline stages to run.

Profilers are split from planners deliberately: a profiler reports
*what the document looks like*; a planner decides *what to do about it*.
This makes both halves cheaply testable and lets a deployment swap
either independently (e.g. a heavyweight LLM-assisted profiler with
the default deterministic planner)."""

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
      * `file_size_bytes` — `os.stat()` size. Useful for cost prediction.
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
    # from the counts so `_compute_quality_scores()` can re-derive
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
        if extension == ".pdf":
            page_count, text_extractable_ratio, has_scanned_pages, pdf_warnings = (
                _safe_pdf_profile(path)
            )
            warnings.extend(pdf_warnings)

        return DocumentProfile(
            document_id=document_id,
            extension=extension,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            text_extractable_ratio=text_extractable_ratio,
            has_images=None,
            has_tables=None,
            has_scanned_pages=has_scanned_pages,
            warnings=tuple(warnings),
        )


def _safe_pdf_profile(
    path: Path,
    *,
    sample_pages: int = 5,
) -> tuple[int | None, float | None, bool | None, list[str]]:
    """Read page count + text-extractability ratio via `pypdf`. Best-effort.

    Samples up to `sample_pages` pages to estimate what fraction have
    embedded text.  Returns:
      (page_count, text_extractable_ratio, has_scanned_pages, warnings)

    `text_extractable_ratio` — 0..1 fraction of sampled pages with
    ≥ 20 non-whitespace chars of extractable text.
    `has_scanned_pages` — True when ratio < 0.1 (nearly all scanned);
    False when ratio ≥ 0.8; None in between (mixed document, planner
    should treat conservatively).

    We deliberately stop at text extraction — no image pixel sampling,
    no table detection. Those are a deeper profiler's job. This gives
    the planner and fast-path logic the cheapest useful signal."""
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:
        warnings.append(
            "pypdf not installed — page_count and text_extractable_ratio unavailable; "
            "planner will fall back to mode-default heuristics"
        )
        return None, None, None, warnings
    try:
        reader = PdfReader(str(path))
        total = len(reader.pages)
        if total == 0:
            return 0, None, None, warnings
        n = min(sample_pages, total)
        pages_with_text = sum(
            1
            for i in range(n)
            if len((reader.pages[i].extract_text() or "").strip()) >= 20
        )
        ratio = round(pages_with_text / n, 2)
        if ratio >= 0.8:
            has_scanned_pages: bool | None = False
        elif ratio < 0.1:
            has_scanned_pages = True
        else:
            has_scanned_pages = None  # mixed — planner decides conservatively
        return total, ratio, has_scanned_pages, warnings
    except Exception as exc:  # noqa: BLE001 — any pypdf error is recoverable
        warnings.append(f"pypdf could not read {path.name!r}: {exc!s}")
        return None, None, None, warnings
