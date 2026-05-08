"""IngestProfileDefinition — central registry for per-profile behavior.

This module is the **read-only documentation surface** for the
profile-to-behavior mapping. It does NOT replace the planner's
`_MODE_ENABLED_STEPS` table or migrate per-profile settings out of
their current homes (RAGAnything settings, EnrichmentSettings, LLM
roles). The framework's existing decision logic continues to live
where it does today; this registry is what operators / developers
read to understand "what does TEXT_ONLY actually mean?".

Why a separate registry instead of fields on `IngestMode`:

  * `IngestMode` is a StrEnum — pure value type, no per-mode
    metadata fits cleanly.
  * Different layers (planner, bridge, enricher, LLM router) each
    own their own slice of profile behavior. This registry projects
    those slices into one document so a developer can answer "what
    runs and what doesn't for TEXT_ONLY?" without reading five
    files.
  * Adding new profiles or refining existing ones happens here,
    not by editing scattered conditionals.

The fields below are the contract operators consume via
`docs/INGESTION_PROFILES.md`. When the framework's actual behavior
diverges from this registry, fix the registry — the running code is
authoritative; this doc is the operator's lens onto it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from j1.processing.planning import (
    IngestMode,
    STEP_COMPILE,
    STEP_ENRICH,
    STEP_GRAPH,
    STEP_INDEX,
)


@dataclass(frozen=True)
class IngestProfileDefinition:
    """Documentation surface for one ingest profile.

    The fields are intentionally descriptive, not prescriptive — the
    running framework's behaviour is the source of truth, and this
    registry is the operator-readable projection of that behaviour.
    """

    mode: IngestMode
    description: str
    intended_for: str
    cost_level: str  # "low" | "medium" | "high"
    latency_level: str  # "fast" | "balanced" | "slow"

    # Parser / compile preferences. The bridge consults
    # `_NATIVE_TEXT_EXTENSIONS` + `_is_text_extractable_pdf` to decide
    # whether to bypass MinerU; this field is informational only.
    avoids_mineru: bool
    parse_method_preference: str  # "auto" | "txt" | "ocr"

    # Step enablement (mirrors `_MODE_ENABLED_STEPS` for visibility).
    enable_compile: bool
    enable_enrich: bool
    enable_graph: bool
    enable_index: bool

    # Per-modality enablement (consulted by `_filter_generic_enrichers`
    # at composite-construction time).
    enable_table_processing: bool
    enable_image_processing: bool
    enable_diagram_processing: bool
    enable_scanned_page_processing: bool
    enable_equation_processing: bool

    # LLM role hints (the planner decides `requires_vision` /
    # `requires_premium_llm` per-document; this is the typical case).
    text_llm_role: str  # "fast" | "text" | "premium"
    requires_vision: bool

    # FE / operator copy.
    operator_notes: tuple[str, ...] = field(default_factory=tuple)


# Per-profile registry. Add a new profile here AND a new entry in
# `_MODE_ENABLED_STEPS` (j1.processing.planning) — the planner reads
# enablement from there, this registry documents intent. Keep the
# two in sync; the conformance test in `test_ingestion_profiles.py`
# guards against drift.
INGEST_PROFILES: Mapping[IngestMode, IngestProfileDefinition] = {
    IngestMode.TEXT_ONLY: IngestProfileDefinition(
        mode=IngestMode.TEXT_ONLY,
        description=(
            "Fast path for plain text and text-layer PDFs. The "
            "bridge bypasses MinerU entirely and feeds bytes "
            "straight into LightRAG's chunker. No images, no "
            "tables, no graph."
        ),
        intended_for=(
            ".txt / .md / .markdown / .rst / .log files; PDFs whose "
            "first 5 pages are ≥80% pypdf-extractable text"
        ),
        cost_level="low",
        latency_level="fast",
        avoids_mineru=True,
        parse_method_preference="txt",
        enable_compile=True,
        enable_enrich=False,
        enable_graph=False,
        enable_index=True,
        enable_table_processing=False,
        enable_image_processing=False,
        enable_diagram_processing=False,
        enable_scanned_page_processing=False,
        enable_equation_processing=False,
        text_llm_role="fast",
        requires_vision=False,
        operator_notes=(
            "MinerU is bypassed — confirm in the run's audit log "
            "that the 'fast-text path' message fired.",
        ),
    ),
    IngestMode.TEXT_WITH_LIGHT_ENRICHMENT: IngestProfileDefinition(
        mode=IngestMode.TEXT_WITH_LIGHT_ENRICHMENT,
        description=(
            "Default for normal business documents with enough "
            "text. Light enrichment runs (classifier, requirement "
            "extractor, source mapper, confidence assessor); "
            "table/image-heavy enrichers stay off unless the "
            "profiler detects them."
        ),
        intended_for="Most PDFs and DOCX files with native text.",
        cost_level="medium",
        latency_level="balanced",
        avoids_mineru=False,
        parse_method_preference="auto",
        enable_compile=True,
        enable_enrich=True,
        enable_graph=False,
        enable_index=True,
        enable_table_processing=False,
        enable_image_processing=False,
        enable_diagram_processing=False,
        enable_scanned_page_processing=False,
        enable_equation_processing=False,
        text_llm_role="text",
        requires_vision=False,
    ),
    IngestMode.TABLE_AWARE: IngestProfileDefinition(
        mode=IngestMode.TABLE_AWARE,
        description=(
            "Documents whose primary content is structured tables "
            "(spreadsheets, CSVs, financial reports). The "
            "TableExtractor enricher runs against compile artifacts; "
            "vision is NOT required unless tables are scanned."
        ),
        intended_for=".xlsx / .xls / .csv / .ods, plus PDFs heavy on tables.",
        cost_level="medium",
        latency_level="balanced",
        avoids_mineru=False,
        parse_method_preference="auto",
        enable_compile=True,
        enable_enrich=True,
        enable_graph=False,
        enable_index=True,
        enable_table_processing=True,
        enable_image_processing=False,
        enable_diagram_processing=False,
        enable_scanned_page_processing=False,
        enable_equation_processing=False,
        text_llm_role="text",
        requires_vision=False,
    ),
    IngestMode.MULTIMODAL_LIGHT: IngestProfileDefinition(
        mode=IngestMode.MULTIMODAL_LIGHT,
        description=(
            "Documents with embedded images / figures but not "
            "dominated by scanned content. Vision LLM is invoked "
            "selectively per image (decorative / logo images "
            "ideally skipped via per-image triage)."
        ),
        intended_for="PDFs with mixed text + figures; presentation decks.",
        cost_level="medium",
        latency_level="balanced",
        avoids_mineru=False,
        parse_method_preference="auto",
        enable_compile=True,
        enable_enrich=True,
        enable_graph=False,
        enable_index=True,
        enable_table_processing=False,
        enable_image_processing=True,
        enable_diagram_processing=True,
        enable_scanned_page_processing=False,
        enable_equation_processing=False,
        text_llm_role="text",
        requires_vision=True,
    ),
    IngestMode.MULTIMODAL_FULL: IngestProfileDefinition(
        mode=IngestMode.MULTIMODAL_FULL,
        description=(
            "Scanned PDFs, image-heavy files, complex layouts. "
            "MinerU's OCR-fallback runs; every visual artifact "
            "goes through the vision LLM. This is the most "
            "expensive profile — use only when the profile signals "
            "demand it."
        ),
        intended_for=(
            "Scanned PDFs, image-only PDFs, technical diagrams, "
            "high-resolution photos with embedded text"
        ),
        cost_level="high",
        latency_level="slow",
        avoids_mineru=False,
        parse_method_preference="auto",  # 'ocr' is implicit in MinerU's auto fallback
        enable_compile=True,
        enable_enrich=True,
        enable_graph=True,
        enable_index=True,
        enable_table_processing=True,
        enable_image_processing=True,
        enable_diagram_processing=True,
        enable_scanned_page_processing=True,
        enable_equation_processing=True,
        text_llm_role="text",
        requires_vision=True,
        operator_notes=(
            "Slowest path. Confirm vision LLM is reachable before "
            "uploading large batches; per-page VLM calls run for "
            "every page MinerU classifies as scanned.",
        ),
    ),
    IngestMode.GRAPH_AWARE: IngestProfileDefinition(
        mode=IngestMode.GRAPH_AWARE,
        description=(
            "Documents where entity / relation discovery is more "
            "important than visual fidelity. Enables the graph "
            "stage explicitly even when the profiler wouldn't have "
            "demanded it on its own. Vision off by default."
        ),
        intended_for=(
            "Knowledge-base documents, research papers, "
            "regulatory filings — anywhere the downstream queries "
            "are 'who did what to whom' rather than 'show me the "
            "diagram'"
        ),
        cost_level="medium",
        latency_level="balanced",
        avoids_mineru=False,
        parse_method_preference="auto",
        enable_compile=True,
        enable_enrich=True,
        enable_graph=True,
        enable_index=True,
        enable_table_processing=True,
        enable_image_processing=False,
        enable_diagram_processing=False,
        enable_scanned_page_processing=False,
        enable_equation_processing=False,
        text_llm_role="text",
        requires_vision=False,
    ),
    IngestMode.FULL_DIAGNOSTIC: IngestProfileDefinition(
        mode=IngestMode.FULL_DIAGNOSTIC,
        description=(
            "Run every safe stage. Persists all intermediate "
            "artifacts. Verbose plan reasons. Intended for "
            "benchmarking, accuracy comparisons, and debugging — "
            "not a production default."
        ),
        intended_for="QA, benchmarks, debugging.",
        cost_level="high",
        latency_level="slow",
        avoids_mineru=False,
        parse_method_preference="auto",
        enable_compile=True,
        enable_enrich=True,
        enable_graph=True,
        enable_index=True,
        enable_table_processing=True,
        enable_image_processing=True,
        enable_diagram_processing=True,
        enable_scanned_page_processing=True,
        enable_equation_processing=True,
        text_llm_role="premium",
        requires_vision=True,
        operator_notes=(
            "Diagnostic only. Never set as the default policy in "
            "production — costs roughly the same as MULTIMODAL_FULL "
            "but with extra verbose audit output.",
        ),
    ),
}


def get_profile(mode: IngestMode) -> IngestProfileDefinition:
    """Lookup the profile definition for an `IngestMode`.

    Raises `KeyError` when the mode isn't registered — adding a new
    `IngestMode` value without a matching `IngestProfileDefinition`
    entry is a contract violation the planner conformance test
    catches at CI time.
    """
    return INGEST_PROFILES[mode]


def expected_steps_for(mode: IngestMode) -> frozenset[str]:
    """Return the step names expected to be enabled by `mode`,
    derived from the profile registry. Used by the conformance test
    to assert parity with the planner's `_MODE_ENABLED_STEPS`."""
    profile = get_profile(mode)
    out: set[str] = set()
    if profile.enable_compile:
        out.add(STEP_COMPILE)
    if profile.enable_enrich:
        out.add(STEP_ENRICH)
    if profile.enable_graph:
        out.add(STEP_GRAPH)
    if profile.enable_index:
        out.add(STEP_INDEX)
    return frozenset(out)
