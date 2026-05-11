"""Normalized compile result.

Typed projection of what the compile activity returned. Adapter
over `ArtifactActivityResult` — preserves the raw vendor output
(via `raw_artifact_refs`) while exposing a stable, named-field
shape downstream consumers (post-compile assessor, FE panels,
final report) can branch on without inspecting nested dicts.

Design rules:
 * Adapter only. Does NOT mutate the activity result, does NOT
 re-parse vendor files. Inputs are the activity result + the
 workflow's retry-history list; outputs are immutable dataclasses.
 * RAGAnything stays a black box. We project its `content_stats` /
 `compile_metrics` dicts onto typed fields and stop there. New
 vendors plug in by populating the same activity-side dicts.
 * Raw artifact refs are preserved by id only — the actual files
 stay in the workspace and are accessible via the existing
 artifact registry. The normalized result is a SUMMARY, not a
 re-encoding of the raw output.
 * Pure. Same activity result + retry history → same normalized
 result, every time. Required for Temporal replay determinism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


__all__ = [
    "COMPILE_ENGINE_RAGANYTHING",
    "CompileAttemptRecord",
    "CompileQualitySignals",
    "ContentRepresentationFlags",
    "DetectedImage",
    "DetectedTable",
    "MetadataPresence",
    "NormalizedCompileResult",
    "build_compile_attempt_records",
    "normalize_compile_result",
]


# Wire vocabulary for `compile_engine`. Mirrors the value used by
# `InitialExecutionPlan` so consumers can branch on the same string.
COMPILE_ENGINE_RAGANYTHING = "raganything"

# Schema version for the persisted normalized result. Bump when
# adding a field whose absence changes consumer behaviour.
_SCHEMA_VERSION = "1"


# ---- Sub-record types -----------------------------------------------


@dataclass(frozen=True)
class DetectedImage:
    """One image the parser surfaced, with its triage decision.

 Sourced from `ArtifactActivityResult.content_stats["images"]` —
 the per-image dict the bridge writes. Only the operational
 fields are typed; vendor-specific metadata stays in the raw
 artifact for deeper consumers."""

    image_id: str
    page: int | None = None
    role: str | None = None
    decision: str | None = None
    caption: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "page": self.page,
            "role": self.role,
            "decision": self.decision,
            "caption": self.caption,
            "score": self.score,
        }


@dataclass(frozen=True)
class DetectedTable:
    """One table the parser surfaced.

 Today the bridge doesn't expose per-table metadata beyond a
 count + page hints — the dataclass is forward-compatible so a
 future parser surfacing structured table descriptors can
 populate `caption` / `row_count` / `column_count` without a
 schema change."""

    table_id: str
    page: int | None = None
    caption: str | None = None
    row_count: int | None = None
    column_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "page": self.page,
            "caption": self.caption,
            "row_count": self.row_count,
            "column_count": self.column_count,
        }


@dataclass(frozen=True)
class MetadataPresence:
    """Metadata-coverage signal the post-compile analyzer reads to
 recommend metadata enrichment.

 Pure data — the analyzer / validation enricher decide how to
 act on a missing field. The post-compile flow populates this
 from `DomainValidationRules.required_metadata_fields` ∩ the
 metadata keys observed in the parsed-content manifest:

 * `required_fields` — the domain's required metadata list
 (copied from the active pack at evaluation time).
 * `present_fields` — keys actually observed in the parsed
 metadata. Empty when the parser surfaced no metadata.
 * `missing_fields` — `required_fields − present_fields`.
 The analyzer uses non-empty `missing_fields` as a
 recommend-enrichment trigger.

 Defaults are empty so packs without `required_metadata_fields`
 contribute a no-op presence record."""

    required_fields: tuple[str, ...] = ()
    present_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_fields": list(self.required_fields),
            "present_fields": list(self.present_fields),
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True)
class ContentRepresentationFlags:
    """Coarse "is this content well represented?" flags the post-
 compile analyzer reads to recommend table/image enrichment.

 Pure data; no heuristic logic on the dataclass. Builder code
 (`normalize_compile_result` + callers) populates these from
 compile signals — e.g. `tables_present_but_unstructured=True`
 when `has_tables=True` but `detected_tables` is empty (parser
 only surfaced a count). The analyzer reads them as additional
 recommend triggers.

 Each flag is three-state (True / False / None=unknown) so a
 pack/parser that doesn't surface the signal can leave it `None`
 and the analyzer treats it as "no signal" rather than "no
 problem"."""

    tables_present_but_unstructured: bool | None = None
    images_present_but_undescribed: bool | None = None
    text_only_but_low_density: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables_present_but_unstructured": self.tables_present_but_unstructured,
            "images_present_but_undescribed": self.images_present_but_undescribed,
            "text_only_but_low_density": self.text_only_but_low_density,
        }


@dataclass(frozen=True)
class CompileQualitySignals:
    """Typed projection of the parser's quality scores.

 Every field 0..1 or None when the parser didn't surface it.
 Consumers (post-compile assessor, final report) branch on
 these to decide enrichment + flag low-quality compiles."""

    parse_quality_score: float | None = None
    text_sufficiency_score: float | None = None
    layout_complexity_score: float | None = None
    empty_page_ratio: float | None = None
    text_extractable_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parse_quality_score": self.parse_quality_score,
            "text_sufficiency_score": self.text_sufficiency_score,
            "layout_complexity_score": self.layout_complexity_score,
            "empty_page_ratio": self.empty_page_ratio,
            "text_extractable_ratio": self.text_extractable_ratio,
        }


@dataclass(frozen=True)
class CompileAttemptRecord:
    """One entry in the compile retry history.

 The workflow builds these in its compile retry loop and threads
 them into `NormalizedCompileResult.retry_history`. Distinct
 from the `j1.processing.compile_retry.CompileAttemptRecord`
 dataclass (which is the retry-evaluator-internal record) — this
 one is the operator-facing audit shape, serialised into the
 persisted compile-result-summary artifact."""

    attempt_number: int
    status: str
    mode: str | None = None
    parser: str | None = None
    parse_method: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    chunks_count: int = 0
    extracted_text_chars: int | None = None
    quality: str | None = None
    retry_reason: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "status": self.status,
            "mode": self.mode,
            "parser": self.parser,
            "parse_method": self.parse_method,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "chunks_count": self.chunks_count,
            "extracted_text_chars": self.extracted_text_chars,
            "quality": self.quality,
            "retry_reason": self.retry_reason,
            "warnings": list(self.warnings),
        }


# ---- Main record ----------------------------------------------------


@dataclass(frozen=True)
class NormalizedCompileResult:
    """Typed normalized projection of one document's compile output.

 Adapter over `ArtifactActivityResult`. Surfaces stable named
 fields downstream consumers (post-compile assessor, FE panels,
 final report) can branch on without re-parsing nested dicts.
 Raw vendor output is preserved by id only — see
 `raw_artifact_refs`."""

    document_id: str
    compile_engine: str = COMPILE_ENGINE_RAGANYTHING
    engine_version: str | None = None
    status: str = "succeeded"

    # Artifact references — raw vendor output stays in the workspace
    # (registered via the artifact registry); consumers fetch by id.
    raw_artifact_refs: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()

    # Counts the post-compile assessor reads.
    chunks_count: int = 0
    extracted_text_chars: int = 0
    page_count: int | None = None
    text_block_count: int | None = None

    # Detection
    detected_tables: tuple[DetectedTable, ...] = ()
    detected_images: tuple[DetectedImage, ...] = ()
    # Sorted vocabulary list ("text" / "images" / "tables" /
    # "equations" / "scanned_pages") — what the parser surfaced.
    detected_content_types: tuple[str, ...] = ()

    # Optional vendor-exposed graph/index artifact refs. Empty when
    # RAGAnything didn't surface graph/index outputs as separate
    # artifacts — the workflow's downstream graph/index activities
    # produce their own when wired.
    graph_artifact_refs: tuple[str, ...] = ()
    index_artifact_refs: tuple[str, ...] = ()

    # Quality
    quality_signals: CompileQualitySignals = field(
        default_factory=CompileQualitySignals,
    )
    # Metadata coverage. Populated by callers that have a domain
    # pack's `validation_rules.required_metadata_fields`; empty
    # otherwise. Drives the analyzer's "missing metadata"
    # enrichment-recommendation trigger.
    metadata_presence: MetadataPresence = field(
        default_factory=MetadataPresence,
    )
    # Coarse content-representation flags. Three-state booleans
    # so `None` means "no signal" rather than "no problem".
    representation_flags: ContentRepresentationFlags = field(
        default_factory=ContentRepresentationFlags,
    )
    # Workflow-side verdict — "good" / "low" / "failed" — from the
    # compile-quality evaluator after the retry loop completes.
    final_quality_verdict: str | None = None

    # Operational
    duration_ms: int | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    retry_history: tuple[CompileAttemptRecord, ...] = ()

    # Final compile mode (mirror of the winning attempt's mode) so
    # consumers don't have to walk retry_history for the common
    # "which mode worked?" question.
    final_compile_mode: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "document_id": self.document_id,
            "compile_engine": self.compile_engine,
            "engine_version": self.engine_version,
            "status": self.status,
            "raw_artifact_refs": list(self.raw_artifact_refs),
            "artifact_kinds": list(self.artifact_kinds),
            "chunks_count": self.chunks_count,
            "extracted_text_chars": self.extracted_text_chars,
            "page_count": self.page_count,
            "text_block_count": self.text_block_count,
            "detected_tables": [t.to_dict() for t in self.detected_tables],
            "detected_images": [i.to_dict() for i in self.detected_images],
            "detected_content_types": list(self.detected_content_types),
            "graph_artifact_refs": list(self.graph_artifact_refs),
            "index_artifact_refs": list(self.index_artifact_refs),
            "quality_signals": self.quality_signals.to_dict(),
            "metadata_presence": self.metadata_presence.to_dict(),
            "representation_flags": self.representation_flags.to_dict(),
            "final_quality_verdict": self.final_quality_verdict,
            "duration_ms": self.duration_ms,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "retry_history": [a.to_dict() for a in self.retry_history],
            "final_compile_mode": self.final_compile_mode,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NormalizedCompileResult":
        return cls(
            document_id=str(payload.get("document_id") or ""),
            compile_engine=str(
                payload.get("compile_engine") or COMPILE_ENGINE_RAGANYTHING
            ),
            engine_version=(
                str(payload["engine_version"])
                if payload.get("engine_version") else None
            ),
            status=str(payload.get("status") or "succeeded"),
            raw_artifact_refs=tuple(payload.get("raw_artifact_refs") or ()),
            artifact_kinds=tuple(payload.get("artifact_kinds") or ()),
            chunks_count=int(payload.get("chunks_count") or 0),
            extracted_text_chars=int(payload.get("extracted_text_chars") or 0),
            page_count=(
                int(payload["page_count"])
                if isinstance(payload.get("page_count"), int) else None
            ),
            text_block_count=(
                int(payload["text_block_count"])
                if isinstance(payload.get("text_block_count"), int) else None
            ),
            detected_tables=tuple(
                DetectedTable(**t)
                for t in (payload.get("detected_tables") or ())
                if isinstance(t, dict)
            ),
            detected_images=tuple(
                DetectedImage(**i)
                for i in (payload.get("detected_images") or ())
                if isinstance(i, dict)
            ),
            detected_content_types=tuple(
                payload.get("detected_content_types") or ()
            ),
            graph_artifact_refs=tuple(payload.get("graph_artifact_refs") or ()),
            index_artifact_refs=tuple(payload.get("index_artifact_refs") or ()),
            quality_signals=CompileQualitySignals(
                **(payload.get("quality_signals") or {})
            ),
            metadata_presence=MetadataPresence(
                **{
                    k: tuple(v) if isinstance(v, list) else v
                    for k, v in (payload.get("metadata_presence") or {}).items()
                }
            ),
            representation_flags=ContentRepresentationFlags(
                **(payload.get("representation_flags") or {})
            ),
            final_quality_verdict=(
                str(payload["final_quality_verdict"])
                if payload.get("final_quality_verdict") else None
            ),
            duration_ms=(
                int(payload["duration_ms"])
                if isinstance(payload.get("duration_ms"), int) else None
            ),
            warnings=tuple(payload.get("warnings") or ()),
            errors=tuple(payload.get("errors") or ()),
            retry_history=tuple(
                CompileAttemptRecord(**{
                    k: (tuple(v) if k == "warnings" else v)
                    for k, v in a.items()
                })
                for a in (payload.get("retry_history") or ())
                if isinstance(a, dict)
            ),
            final_compile_mode=(
                str(payload["final_compile_mode"])
                if payload.get("final_compile_mode") else None
            ),
        )


# ---- Builder ---------------------------------------------------------


def normalize_compile_result(
    activity_result: Any,
    *,
    document_id: str,
    retry_attempts: list[dict[str, Any]] | None = None,
    final_quality_verdict: str | None = None,
    duration_ms: int | None = None,
    compile_engine: str = COMPILE_ENGINE_RAGANYTHING,
    engine_version: str | None = None,
    extra_warnings: tuple[str, ...] = (),
    graph_artifact_refs: tuple[str, ...] = (),
    index_artifact_refs: tuple[str, ...] = (),
    metadata_presence: MetadataPresence | None = None,
) -> NormalizedCompileResult:
    """Build a `NormalizedCompileResult` from the activity result.

 Pure adapter — reads `content_stats` + `compile_metrics` + the
 artifact_id list off the activity result and projects them onto
 typed fields. Does NOT call any LLM, OCR, vision, or vendor API.

 `retry_attempts` is the workflow's per-attempt audit list (same
 shape used today by `compile_strategy_report`). When None, the
 `retry_history` field is empty — useful for callers that don't
 track retries (single-attempt paths)."""
    content_stats: dict[str, Any] = dict(
        getattr(activity_result, "content_stats", None) or {}
    )
    compile_metrics: dict[str, Any] = dict(
        getattr(activity_result, "compile_metrics", None) or {}
    )

    status = str(getattr(activity_result, "status", "succeeded") or "succeeded")
    artifact_ids = tuple(getattr(activity_result, "artifact_ids", ()) or ())
    artifact_kinds = tuple(getattr(activity_result, "kinds", ()) or ())

    chunks_count = _coerce_int(
        compile_metrics.get("chunks_count"),
        default=sum(1 for k in artifact_kinds if k == "chunk"),
    )
    extracted_text_chars = _coerce_int(
        compile_metrics.get("extracted_text_chars"),
        default=_coerce_int(content_stats.get("total_text_chars"), default=0),
    )
    page_count = _coerce_optional_int(content_stats.get("page_count"))
    text_block_count = _coerce_optional_int(content_stats.get("text_block_count"))

    detected_images = _build_detected_images(content_stats.get("images"))
    detected_tables = _build_detected_tables(content_stats)
    detected_types = _build_detected_content_types(content_stats)

    quality = CompileQualitySignals(
        parse_quality_score=_coerce_optional_float(
            content_stats.get("parse_quality_score"),
        ),
        text_sufficiency_score=_coerce_optional_float(
            content_stats.get("text_sufficiency_score"),
        ),
        layout_complexity_score=_coerce_optional_float(
            content_stats.get("layout_complexity_score"),
        ),
        empty_page_ratio=_coerce_optional_float(
            content_stats.get("empty_page_ratio"),
        ),
        text_extractable_ratio=_coerce_optional_float(
            content_stats.get("text_extractable_ratio"),
        ),
    )

    warnings: list[str] = []
    plan_warnings = compile_metrics.get("plan_warnings") or ()
    if isinstance(plan_warnings, (list, tuple)):
        warnings.extend(str(w) for w in plan_warnings if w)
    warnings.extend(extra_warnings)

    errors: list[str] = []
    error_msg = getattr(activity_result, "error", None)
    if error_msg:
        errors.append(str(error_msg))

    retry_history = build_compile_attempt_records(retry_attempts or [])
    final_compile_mode = (
        retry_history[-1].mode if retry_history else None
    )
    if final_compile_mode is None:
        mode_from_metrics = compile_metrics.get("assessment_mode")
        if isinstance(mode_from_metrics, str):
            final_compile_mode = mode_from_metrics

    # Derive content-representation flags from the projected signals.
    # Pure heuristic; analyzer reads them as additional recommend
    # triggers. Three-state: True / False / None means "no signal".
    flags = _derive_representation_flags(
        content_stats=content_stats,
        detected_tables=detected_tables,
        detected_images=detected_images,
        text_block_count=text_block_count,
        page_count=page_count,
    )

    return NormalizedCompileResult(
        document_id=document_id,
        compile_engine=compile_engine,
        engine_version=engine_version,
        status=status,
        raw_artifact_refs=artifact_ids,
        artifact_kinds=artifact_kinds,
        chunks_count=chunks_count,
        extracted_text_chars=extracted_text_chars,
        page_count=page_count,
        text_block_count=text_block_count,
        detected_tables=detected_tables,
        detected_images=detected_images,
        detected_content_types=detected_types,
        graph_artifact_refs=graph_artifact_refs,
        index_artifact_refs=index_artifact_refs,
        quality_signals=quality,
        metadata_presence=metadata_presence or MetadataPresence(),
        representation_flags=flags,
        final_quality_verdict=final_quality_verdict,
        duration_ms=duration_ms,
        warnings=tuple(warnings),
        errors=tuple(errors),
        retry_history=retry_history,
        final_compile_mode=final_compile_mode,
    )


def _derive_representation_flags(
    *,
    content_stats: dict[str, Any],
    detected_tables: tuple[DetectedTable, ...],
    detected_images: tuple[DetectedImage, ...],
    text_block_count: int | None,
    page_count: int | None,
) -> ContentRepresentationFlags:
    """Compute the coarse "well-represented?" flags from compile
 signals. Pure / deterministic.

 Rules:
 * tables_present_but_unstructured = True when the bridge
 surfaced a table_count > 0 but the descriptor list is empty
 (placeholder fallback used). Means: tables exist but we
 only know the count, so structured-table enrichment would
 add value.
 * images_present_but_undescribed = True when images exist
 but none carry a `caption` field. The image enricher can
 add captions to make these retrievable.
 * text_only_but_low_density = True when no tables/images and
 the page-averaged text density looks thin (<400 chars/page).
 Flags long scanned-text-heavy docs that may need additional
 retrieval metadata.
 """
    table_count = content_stats.get("table_count")
    image_count = content_stats.get("image_count")

    tables_unstructured: bool | None = None
    if isinstance(table_count, int):
        if table_count > 0 and all(
            t.caption is None and t.row_count is None
            for t in detected_tables
        ):
            tables_unstructured = True
        elif table_count > 0:
            tables_unstructured = False

    images_undescribed: bool | None = None
    if isinstance(image_count, int):
        if image_count > 0 and detected_images and all(
            i.caption is None for i in detected_images
        ):
            images_undescribed = True
        elif image_count > 0:
            images_undescribed = False

    text_only_low_density: bool | None = None
    has_tables = bool(table_count and table_count > 0)
    has_images = bool(image_count and image_count > 0)
    if not has_tables and not has_images and page_count and page_count > 0:
        chars = content_stats.get("total_text_chars")
        if isinstance(chars, int):
            text_only_low_density = (chars / page_count) < 400

    return ContentRepresentationFlags(
        tables_present_but_unstructured=tables_unstructured,
        images_present_but_undescribed=images_undescribed,
        text_only_but_low_density=text_only_low_density,
    )


def build_compile_attempt_records(
    attempts: list[dict[str, Any]],
) -> tuple[CompileAttemptRecord, ...]:
    """Project the workflow's per-attempt audit dicts into typed
 `CompileAttemptRecord` instances. Tolerant of unknown keys —
 unrecognised fields are dropped; missing fields fall back to
 the dataclass defaults."""
    records: list[CompileAttemptRecord] = []
    for entry in attempts:
        if not isinstance(entry, dict):
            continue
        raw_warnings = entry.get("warnings") or ()
        if isinstance(raw_warnings, (list, tuple)):
            warnings_tuple = tuple(str(w) for w in raw_warnings if w)
        else:
            warnings_tuple = ()
        records.append(CompileAttemptRecord(
            attempt_number=_coerce_int(entry.get("attempt_number"), default=0),
            status=str(entry.get("status") or "unknown"),
            mode=(
                str(entry["mode"]) if entry.get("mode") else None
            ),
            parser=(
                str(entry["parser"]) if entry.get("parser") else None
            ),
            parse_method=(
                str(entry["parse_method"])
                if entry.get("parse_method") else None
            ),
            started_at=(
                str(entry["started_at"])
                if entry.get("started_at") else None
            ),
            completed_at=(
                str(entry["completed_at"])
                if entry.get("completed_at") else None
            ),
            chunks_count=_coerce_int(entry.get("chunks_count"), default=0),
            extracted_text_chars=_coerce_optional_int(
                entry.get("extracted_text_chars"),
            ),
            quality=(
                str(entry["quality"]) if entry.get("quality") else None
            ),
            retry_reason=(
                str(entry["retry_reason"])
                if entry.get("retry_reason") else None
            ),
            warnings=warnings_tuple,
        ))
    return tuple(records)


# ---- Helpers --------------------------------------------------------


def _build_detected_images(
    raw: Any,
) -> tuple[DetectedImage, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[DetectedImage] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(DetectedImage(
            image_id=str(
                entry.get("image_id") or entry.get("id") or "unknown"
            ),
            page=_coerce_optional_int(entry.get("page") or entry.get("page_idx")),
            role=(
                str(entry["role"]) if entry.get("role") else None
            ),
            decision=(
                str(entry["decision"]) if entry.get("decision") else None
            ),
            caption=(
                str(entry["caption"]) if entry.get("caption") else None
            ),
            score=_coerce_optional_float(entry.get("score")),
        ))
    return tuple(out)


def _build_detected_tables(
    content_stats: dict[str, Any],
) -> tuple[DetectedTable, ...]:
    """Project tables from `content_stats`.

 The bridge today surfaces only a `table_count` — no per-table
 descriptors. We emit one placeholder `DetectedTable` per count
 so the FE can render a 'N tables detected' tile without
 fabricating page/caption info. Future parsers populating
 `content_stats["tables"]` with descriptor dicts get full typed
 records via the same path."""
    raw = content_stats.get("tables")
    if isinstance(raw, (list, tuple)):
        out: list[DetectedTable] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            out.append(DetectedTable(
                table_id=str(
                    entry.get("table_id") or entry.get("id") or "unknown"
                ),
                page=_coerce_optional_int(
                    entry.get("page") or entry.get("page_idx"),
                ),
                caption=(
                    str(entry["caption"]) if entry.get("caption") else None
                ),
                row_count=_coerce_optional_int(entry.get("row_count")),
                column_count=_coerce_optional_int(entry.get("column_count")),
            ))
        return tuple(out)
    count = _coerce_optional_int(content_stats.get("table_count"))
    if count and count > 0:
        return tuple(
            DetectedTable(table_id=f"table-{i + 1}") for i in range(count)
        )
    return ()


def _build_detected_content_types(
    content_stats: dict[str, Any],
) -> tuple[str, ...]:
    """Sorted vocabulary list of content types the parser saw.
 Mirrors `_build_extraction_evidence`'s detected list so the FE
 can branch on the same strings whether reading the new
 normalized result or the legacy strategy report."""
    detected: list[str] = []
    text_chars = _coerce_optional_int(content_stats.get("total_text_chars"))
    text_blocks = _coerce_optional_int(content_stats.get("text_block_count"))
    if (
        content_stats.get("has_text") is True
        or (text_blocks is not None and text_blocks > 0)
        or (text_chars is not None and text_chars > 0)
    ):
        detected.append("text")
    if content_stats.get("has_images") is True or _is_positive(
        content_stats.get("image_count"),
    ):
        detected.append("images")
    if content_stats.get("has_tables") is True or _is_positive(
        content_stats.get("table_count"),
    ):
        detected.append("tables")
    if content_stats.get("has_equations") is True or _is_positive(
        content_stats.get("equation_count"),
    ):
        detected.append("equations")
    if content_stats.get("has_scanned_pages") is True:
        detected.append("scanned_pages")
    return tuple(detected)


def _is_positive(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
