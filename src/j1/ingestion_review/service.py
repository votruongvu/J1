"""IngestionResultReviewService — read-only review surface.

Composes data from `IngestionRunStore`, `ArtifactRegistry`, the audit
log, and the workspace into UI-friendly DTOs. Does NOT touch
`RetrievalService` — review of ingestion outputs is a distinct
responsibility from runtime retrieval.

Tenant/project/run/artifact ownership is enforced on every call. Any
mismatch raises `ReviewNotFound` so the REST layer returns a uniform
404 (cross-tenant probing can't tell "missing" from "forbidden").
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError, ArtifactRegistry
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.errors.exceptions import PathTraversalError
from j1.ingestion_review.availability import (
    graph_unavailable_reason,
    resolve_available_views,
)
from j1.ingestion_review.dtos import (
    ArtifactPageDTO,
    ArtifactRecordDTO,
    ChunkDetailDTO,
    ChunkPageDTO,
    GraphSnapshotDTO,
    QualityReportDTO,
    QualitySummaryDTO,
    RunSummaryDTO,
    StepErrorDTO,
    StepResultDTO,
    WarningDTO,
)
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.ingestion_review.projectors import (
    ChunkProjector,
    GraphSnapshotProjector,
    QualityReportProjector,
)
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.ingestion_review.projectors.graph import GRAPH_KIND
from j1.projects.context import ProjectContext
from j1.runs import PROGRESS_ACTION_PREFIX
from j1.runs.models import IngestionRun
from j1.runs.store import IngestionRunStore
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.ingestion_review")

# Severities recognized as "warnings" in audit progress payloads. The
# reporter writes these as upper-cased strings; we lower-case for the
# DTO so FE palette keys are stable.
_WARNING_SEVERITIES = frozenset({"WARNING", "ERROR"})

# Pagination limits: identical to `/ingestion-runs` so the FE only
# needs one Page component. `MAX_PAGE_SIZE` is enforced in the REST
# handler (FastAPI Query(le=...)); the service trusts what it gets.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Per-list caps for the graph snapshot endpoint. The defaults match
# what the FE will request; the absolute max keeps a malicious caller
# from materialising a 200k-edge response. The REST handler enforces
# the upper bound via FastAPI Query(le=...).
DEFAULT_GRAPH_MAX_NODES = 5000
DEFAULT_GRAPH_MAX_EDGES = 5000
ABS_MAX_GRAPH_NODES = 50_000
ABS_MAX_GRAPH_EDGES = 50_000


# ---- Content-type derivation -----------------------------------------
#
# Map artifact-file extensions to media types. Anything outside this
# table falls back to `application/octet-stream` and is served with
# `Content-Disposition: attachment` (download-only). The list is
# deliberately narrow — adding a type here is a one-line opt-in for
# inline rendering on the FE; everything else stays a download.
_INLINE_MEDIA_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}
_OCTET_STREAM = "application/octet-stream"


def _derive_media_type(location: str) -> tuple[str, bool]:
    """Return `(media_type, is_inline)` for an artifact location.

    `is_inline=False` means the FE should download the file rather
    than try to render it; the REST handler sets `Content-Disposition:
    attachment` for those."""
    ext = PurePosixPath(location).suffix.lower()
    media = _INLINE_MEDIA_TYPES.get(ext)
    if media is None:
        return _OCTET_STREAM, False
    return media, True


@dataclass(frozen=True)
class ArtifactContent:
    """Bytes + metadata for one artifact, returned by
    `read_run_artifact_content`. The REST handler turns this into a
    `Response`; the service stays framework-agnostic."""

    artifact_id: str
    bytes: bytes
    media_type: str
    is_inline: bool
    filename: str
    content_hash: str
    byte_size: int


class IngestionResultReviewService:
    """Read-only review surface for completed ingestion runs.

    Constructor takes the data sources directly — no facade, no
    container — so the wiring layer is explicit and the service is
    trivially constructable in tests."""

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        workspace: WorkspaceResolver,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._workspace = workspace

    # ---- Run summary --------------------------------------------------

    def summarize_run(self, ctx: ProjectContext, run_id: str) -> RunSummaryDTO:
        """Build a Results-tab Overview projection for the given run.

        Returns a `RunSummaryDTO` with the data the FE needs to render
        the Overview tab AND decide which other tabs to enable
        (`available_views`).

        Raises `ReviewNotFound` when the run doesn't exist in the
        caller's tenant/project."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        warnings = self._read_warnings(ctx, run_id)

        steps = _coerce_step_results(run.metadata.get("step_results"))
        artifact_counts = _count_by_kind(artifacts)
        total_bytes = sum(a.byte_size for a in artifacts)
        duration_ms = _duration_ms(run)
        document_ids = _document_ids(run)
        quality_summary = _quality_summary(run, warnings)

        return RunSummaryDTO(
            run_id=run.run_id,
            status=str(run.status),
            duration_ms=duration_ms,
            document_ids=document_ids,
            steps=steps,
            artifact_counts=artifact_counts,
            total_bytes=total_bytes,
            warnings=warnings,
            quality_summary=quality_summary,
            available_views=resolve_available_views(run, artifacts),
        )

    # ---- Run-scoped artifact list ------------------------------------

    def list_run_artifacts(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        kind: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ArtifactPageDTO:
        """Return artifacts produced by `run`, paginated.

        Filtering by `kind` happens AFTER run-scoping — the page count
        always reflects the run's filtered set, never the project-wide
        artifact count. Ordering is `created_at` ascending so re-fetching
        a page yields the same items even if new artifacts arrive."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        if kind is not None:
            artifacts = [a for a in artifacts if a.kind == kind]
        artifacts.sort(key=lambda a: (a.created_at, a.artifact_id))

        page = max(page, 1)
        page_size = max(min(page_size, MAX_PAGE_SIZE), 1)
        total = len(artifacts)
        start = (page - 1) * page_size
        items = artifacts[start : start + page_size]

        return ArtifactPageDTO(
            items=[_artifact_record_to_dto(a) for a in items],
            page=page,
            page_size=page_size,
            total=total,
        )

    # ---- Run-scoped artifact content ---------------------------------

    def read_run_artifact_content(
        self,
        ctx: ProjectContext,
        run_id: str,
        artifact_id: str,
    ) -> ArtifactContent:
        """Read the bytes for one artifact, verifying full ownership.

        Ownership chain: tenant + project (from `ctx`) → run (must
        exist in ctx) → artifact (must belong to the run). Any break
        raises `ReviewNotFound` so cross-tenant / cross-run probing
        looks identical to "missing"."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        record = _find_artifact(artifacts, artifact_id)
        if record is None:
            raise ReviewNotFound(
                f"artifact {artifact_id!r} not found for run {run_id!r}"
            )

        path = self._resolve_artifact_path(ctx, record)
        if not path.is_file():
            # Registry has the record but the bytes are gone (manual
            # cleanup, partial restore, …). Same shape as "not found"
            # — the FE shouldn't have to distinguish, and we don't
            # want to leak filesystem state.
            raise ReviewNotFound(
                f"artifact {artifact_id!r} content not found on disk"
            )

        media_type, is_inline = _derive_media_type(record.location)
        data = path.read_bytes()
        filename = PurePosixPath(record.location).name or record.artifact_id
        return ArtifactContent(
            artifact_id=record.artifact_id,
            bytes=data,
            media_type=media_type,
            is_inline=is_inline,
            filename=filename,
            content_hash=record.content_hash,
            byte_size=len(data),
        )

    # ---- Chunk projection -------------------------------------------

    def list_run_chunks(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        status: str | None = None,
        min_confidence: float | None = None,
    ) -> ChunkPageDTO:
        """Return chunks produced by the run, paginated.

        `status` is matched against `chunk.metadata["status"]` (case-
        insensitive). `min_confidence` drops chunks whose `confidence`
        is below the threshold OR missing — so the filter is a strict
        floor, never a "show me chunks I don't know about" loophole."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        records = self._project_chunks(ctx, artifacts)
        records = _filter_chunks(
            records, status=status, min_confidence=min_confidence,
        )

        page = max(page, 1)
        page_size = max(min(page_size, MAX_PAGE_SIZE), 1)
        total = len(records)
        start = (page - 1) * page_size
        slice_ = records[start : start + page_size]

        projector = ChunkProjector(path_resolver=self._artifact_path_resolver(ctx))
        return ChunkPageDTO(
            items=[projector.to_preview(r) for r in slice_],
            page=page,
            page_size=page_size,
            total=total,
        )

    def get_run_chunk(
        self, ctx: ProjectContext, run_id: str, chunk_id: str,
    ) -> ChunkDetailDTO:
        """Return one chunk in detail view (full body + lineage).

        Scans every chunk in the run; this is fine for typical document
        sizes (hundreds to a few thousand chunks). If a deployment grows
        past that, the projector should add a per-request index — left
        for a follow-up."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        records = self._project_chunks(ctx, artifacts)
        for record in records:
            if record.chunk_id == chunk_id:
                lineage = {
                    "documentIds": list(record.source_document_ids),
                    "sourceArtifactId": record.source_artifact_id,
                    "stage": "compile",
                }
                return ChunkProjector.to_detail(record, lineage=lineage)
        raise ReviewNotFound(
            f"chunk {chunk_id!r} not found for run {run_id!r}"
        )

    def iter_run_chunks_ndjson(
        self, ctx: ProjectContext, run_id: str,
    ) -> Iterable[bytes]:
        """Return a streaming NDJSON byte iterator, one line per chunk.

        Validation (`_load_run` → ownership check → projection) runs
        EAGERLY at call time so a `ReviewNotFound` propagates BEFORE
        the REST handler hands the iterator to `StreamingResponse`.
        Otherwise the response would already be 200 by the time the
        generator's first `next()` raised."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        records = self._project_chunks(ctx, artifacts)

        def _stream() -> Iterable[bytes]:
            for record in records:
                preview = ChunkProjector.to_preview(record)
                line = preview.model_dump_json(by_alias=True).encode("utf-8")
                yield line + b"\n"

        return _stream()

    # ---- Quality report ---------------------------------------------

    def get_run_quality_report(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        include_raw: bool = False,
    ) -> QualityReportDTO:
        """Compose the run's quality report as a neutral DTO.

        Inputs (composed inside the projector):
          * `enriched.confidence_assessment` artifacts → overall +
            modality + low-confidence findings.
          * `enriched.consistency_findings` artifacts →
            low-confidence findings (consistency side).
          * Audit-log warnings for the run → `warnings[]`.
          * Persisted `step_results` → `skippedSteps[]` /
            `failedOptionalSteps[]`.

        `include_raw=True` populates the optional `rawDebug` field with
        the unprojected source JSON. Off by default — vendor-shaped
        payloads should never reach the FE through the standard
        contract."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        warnings = self._read_warnings(ctx, run_id)
        step_results_raw = run.metadata.get("step_results")
        step_results = step_results_raw if isinstance(step_results_raw, list) else []

        projector = QualityReportProjector(
            path_resolver=self._artifact_path_resolver(ctx),
        )
        return projector.project(
            artifacts,
            warnings=warnings,
            step_results=step_results,
            include_raw=include_raw,
        )

    # ---- Graph snapshot ---------------------------------------------

    def get_run_graph(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        max_nodes: int = DEFAULT_GRAPH_MAX_NODES,
        max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
    ) -> GraphSnapshotDTO:
        """Project the run's graph_json artifacts into a neutral DTO.

        When the run produced no graph artifacts, the snapshot's
        `unavailable.reason` is populated with the same copy used by
        `availableViews.graph.reason` in the run summary — single
        source of truth via `graph_unavailable_reason()`.

        `max_nodes` / `max_edges` are per-list caps applied in the
        projector. The REST handler clamps them to
        `ABS_MAX_GRAPH_NODES` / `ABS_MAX_GRAPH_EDGES` upstream."""
        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)

        unavailable_reason: str | None = None
        if not any(a.kind == GRAPH_KIND for a in artifacts):
            unavailable_reason = graph_unavailable_reason(run)

        projector = GraphSnapshotProjector(
            path_resolver=self._artifact_path_resolver(ctx),
        )
        return projector.project(
            artifacts,
            max_nodes=max_nodes,
            max_edges=max_edges,
            unavailable_reason=unavailable_reason,
        )

    # ---- Internals ----------------------------------------------------

    def _project_chunks(
        self, ctx: ProjectContext, artifacts: list[ArtifactRecord],
    ) -> list[_ChunkRecord]:
        """Run the chunk projector against this run's artifacts.

        Centralized so list / detail / export all see the same chunks
        in the same order — pagination invariants depend on this."""
        projector = ChunkProjector(path_resolver=self._artifact_path_resolver(ctx))
        return projector.project_records(artifacts)

    def _artifact_path_resolver(self, ctx: ProjectContext):
        """Closure binding the path-traversal guard to the caller's
        context. Passed to projectors so they can stay workspace-
        agnostic — used by both `ChunkProjector` and
        `QualityReportProjector`."""
        def _resolve(record: ArtifactRecord) -> Path:
            return self._resolve_artifact_path(ctx, record)
        return _resolve

    def _resolve_artifact_path(
        self, ctx: ProjectContext, record: ArtifactRecord,
    ) -> Path:
        """Resolve `record.location` to an absolute path on disk, with
        a defense-in-depth path-traversal guard.

        `location` is registry-controlled (we wrote it via
        `ProcessingService._register_draft` as `f"{area}/{filename}"`),
        but a tampered registry — or a future producer that writes
        `..` into the field — must not be able to escape the project
        workspace. Two checks:

          1. The first path segment must name a known `WorkspaceArea`.
          2. The resolved path must stay within the area directory."""
        location = record.location.strip()
        if not location:
            raise ReviewNotFound("artifact has no location")
        parts = PurePosixPath(location).parts
        if len(parts) < 2:
            # `<area>/<filename>` is the contract; anything shorter
            # can't be valid. Treat as not-found rather than 500.
            raise ReviewNotFound("artifact location malformed")
        area_name, *rest = parts
        try:
            area = WorkspaceArea(area_name)
        except ValueError as exc:
            raise ReviewNotFound(
                f"artifact location uses unknown area {area_name!r}"
            ) from exc

        area_root = self._workspace.area(ctx, area).resolve()
        candidate = (area_root.joinpath(*rest)).resolve()
        try:
            candidate.relative_to(area_root)
        except ValueError as exc:
            # Path traversal attempt — surface a typed error so audit
            # / monitoring can pick it up, but the REST layer maps it
            # to 404 (uniform with the other not-found shape).
            raise PathTraversalError(
                f"resolved artifact path {candidate} escapes area {area_root}"
            ) from exc
        return candidate

    def _load_run(self, ctx: ProjectContext, run_id: str) -> IngestionRun:
        run = self._run_store.get(ctx, run_id)
        if run is None:
            # Identical message regardless of cause (missing vs.
            # cross-tenant) so existence isn't probeable.
            raise ReviewNotFound(f"ingestion run {run_id!r} not found")
        return run

    def _resolve_run_artifacts(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> list[ArtifactRecord]:
        """Return artifacts produced by `run`.

        Two strategies, applied in order:

          1. **Direct tag** — match `record.metadata["run_id"] == run.run_id`.
             Fast path for runs whose artifacts were tagged at registration
             time (Phase 4).

          2. **Lineage fallback** — match any artifact whose
             `source_document_ids` intersects the run's target document set.
             Covers legacy artifacts written before tagging shipped, and
             keeps the surface working with no migration."""
        all_artifacts = self._artifacts.list_artifacts(ctx)

        target_doc_ids = set(_document_ids(run))

        tagged: list[ArtifactRecord] = []
        lineage: list[ArtifactRecord] = []
        for record in all_artifacts:
            tagged_run_id = record.metadata.get("run_id")
            if tagged_run_id == run.run_id:
                tagged.append(record)
                continue
            if tagged_run_id is not None and tagged_run_id != run.run_id:
                # Tagged for a different run — never include via lineage.
                continue
            if target_doc_ids and target_doc_ids.intersection(record.source_document_ids):
                lineage.append(record)

        # Tagged matches are authoritative; fall back to lineage when no
        # tagged artifacts exist for this run.
        return tagged if tagged else lineage

    def _read_warnings(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[WarningDTO]:
        """Read WARNING/ERROR-severity progress events for the run.

        Reads the JSONL audit log directly using the same pattern as
        `_read_progress_events` in the REST adapter — duplication is
        intentional for Phase 1 (small, isolated surface). Phase 5 may
        extract a shared `AuditLogReader` once the quality projector
        also needs it."""
        path = self._workspace.audit(ctx) / AUDIT_LOG_FILENAME
        if not path.exists():
            return []
        warnings: list[WarningDTO] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("correlation_id") != run_id:
                continue
            action = data.get("action") or ""
            if not action.startswith(PROGRESS_ACTION_PREFIX):
                continue
            payload = data.get("payload") or {}
            severity = str(payload.get("severity") or "").upper()
            if severity not in _WARNING_SEVERITIES:
                continue
            warnings.append(
                WarningDTO(
                    code=action[len(PROGRESS_ACTION_PREFIX):],
                    message=str(payload.get("message") or ""),
                    severity=severity.lower(),
                    step=payload.get("step"),
                    document_id=payload.get("document_id"),
                    page=payload.get("page"),
                    chunk_id=payload.get("chunk_id"),
                    artifact_id=payload.get("artifact_id"),
                )
            )
        return warnings


# ---- Helpers (module-level so they're easy to unit-test) ----------


def _find_artifact(
    artifacts: list[ArtifactRecord], artifact_id: str,
) -> ArtifactRecord | None:
    for record in artifacts:
        if record.artifact_id == artifact_id:
            return record
    return None


def _artifact_record_to_dto(record: ArtifactRecord) -> ArtifactRecordDTO:
    return ArtifactRecordDTO(
        artifact_id=record.artifact_id,
        kind=record.kind,
        location=record.location,
        content_hash=record.content_hash,
        byte_size=record.byte_size,
        status=str(record.status),
        review_status=str(record.review_status),
        version=record.version,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        source_document_ids=list(record.source_document_ids),
        source_artifact_ids=list(record.source_artifact_ids),
        metadata=dict(record.metadata),
    )


def _document_ids(run: IngestionRun) -> list[str]:
    """Best-effort recovery of the document set this run covered.

    `IngestionRun.document_id` is always present; runs that target
    multiple documents may also list them under
    `metadata["target_document_ids"]`."""
    raw = run.metadata.get("target_document_ids")
    if isinstance(raw, list) and raw:
        seen: list[str] = []
        for entry in raw:
            text = str(entry)
            if text and text not in seen:
                seen.append(text)
        if run.document_id and run.document_id not in seen:
            seen.append(run.document_id)
        return seen
    return [run.document_id] if run.document_id else []


def _duration_ms(run: IngestionRun) -> int | None:
    if run.completed_at is None or run.started_at is None:
        return None
    delta = run.completed_at - run.started_at
    return int(delta.total_seconds() * 1000)


def _count_by_kind(artifacts: Iterable[ArtifactRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in artifacts:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return counts


def _filter_chunks(
    records: list[_ChunkRecord],
    *,
    status: str | None,
    min_confidence: float | None,
) -> list[_ChunkRecord]:
    """Apply optional list-endpoint filters.

    `status` is matched case-insensitively against `metadata["status"]`
    (a free-form field producers may set). `min_confidence` is a
    strict floor — chunks without a confidence score are excluded
    when the filter is active."""
    if status is None and min_confidence is None:
        return records
    needle = status.strip().lower() if status else None
    out: list[_ChunkRecord] = []
    for record in records:
        if needle is not None:
            value = record.metadata.get("status")
            if not isinstance(value, str) or value.strip().lower() != needle:
                continue
        if min_confidence is not None:
            if record.confidence is None or record.confidence < min_confidence:
                continue
        out.append(record)
    return out


def _coerce_step_results(raw: object) -> list[StepResultDTO]:
    """Hydrate step results persisted in run metadata.

    The workflow writes these as plain dicts (Phase 4); be liberal
    about shape so a partial write or schema drift doesn't blow up
    the whole summary endpoint."""
    if not isinstance(raw, list):
        return []
    out: list[StepResultDTO] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            error = entry.get("error")
            error_dto: StepErrorDTO | None = None
            if isinstance(error, dict):
                error_dto = StepErrorDTO(
                    type=str(error.get("type") or ""),
                    message=str(error.get("message") or ""),
                    retryable=bool(error.get("retryable") or False),
                )
            out.append(
                StepResultDTO(
                    step=str(entry.get("step") or ""),
                    status=str(entry.get("status") or ""),
                    required=bool(entry.get("required") or False),
                    source=str(entry.get("source") or ""),
                    started_at=_str_or_none(entry.get("started_at")),
                    completed_at=_str_or_none(entry.get("completed_at")),
                    duration_ms=_int_or_none(entry.get("duration_ms")),
                    reason=_str_or_none(entry.get("reason")),
                    error=error_dto,
                    artifact_count=int(entry.get("artifact_count") or 0),
                    metadata=dict(entry.get("metadata") or {}),
                )
            )
        except Exception:  # pragma: no cover — defensive, never observed
            _log.warning("dropped malformed step result during summary projection")
            continue
    return out


def _quality_summary(
    run: IngestionRun, warnings: list[WarningDTO],
) -> QualitySummaryDTO | None:
    """Tiny projection for the Overview scorecard.

    Returns None when there's nothing useful to show; the FE then
    omits the scorecard rather than rendering an empty box."""
    overall = run.metadata.get("overall_confidence")
    overall_value: float | None = None
    if isinstance(overall, (int, float)):
        overall_value = float(overall)

    if overall_value is None and not warnings:
        return None
    return QualitySummaryDTO(
        overall_confidence=overall_value,
        warning_count=len(warnings),
        low_confidence_count=0,
    )


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Suppress "imported but unused" warnings on the optional deps; they are
# part of the public surface for follow-up phases that consume them.
_ = ArtifactNotFoundError
