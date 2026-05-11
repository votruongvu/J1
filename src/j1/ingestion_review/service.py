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
from typing import Any, TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from j1.processing.planning_settings import PlanningSettings

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
    ContentInventoryDTO,
    ContentInventoryItemDTO,
    ContentInventorySourceDTO,
    ContentInventorySummaryDTO,
    GraphSnapshotDTO,
    PlanningAssessmentDTO,
    PlanningContentDigestDTO,
    PlanningLLMRecommendationDTO,
    PlanningResultDTO,
    PlanningStepDecisionDTO,
    QualityReportDTO,
    QualitySummaryDTO,
    RunSummaryDTO,
    StepErrorDTO,
    StepResultDTO,
    WarningDTO,
)
from j1.ingestion_review.exceptions import ReviewNotFound, RunStillActive
from j1.ingestion_review.projectors import (
    ChunkProjector,
    GraphSnapshotProjector,
    QualityReportProjector,
)
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.ingestion_review.projectors.graph import GRAPH_KIND
from j1.projects.context import ProjectContext
from j1.runs import (
    ACTION_PROGRESS_PLAN_GENERATED,
    ACTION_PROGRESS_PLAN_REVISED,
    PROGRESS_ACTION_PREFIX,
)
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
        planning_settings: "PlanningSettings | None" = None,
    ) -> None:
        from j1.processing.planning_settings import (
            PlanningSettings as _PlanningSettings,
        )

        self._run_store = run_store
        self._artifacts = artifact_registry
        self._workspace = workspace
        # `planning_settings=None` keeps existing call sites working;
        # we substitute the safe defaults so the projector always has
        # the cap fields it needs.
        self._planning_settings = planning_settings or _PlanningSettings()

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
        # Diagnostic log — operator-facing snapshot of what the
        # resolver found for this run. Surfaced when the run-detail
        # page renders an unexpected empty state.
        import logging as _logging
        _logging.getLogger("j1.ingestion_review").info(
            "summarize_run: run_id=%s status=%s kinds=%s",
            run_id, run.status,
            sorted({a.kind for a in artifacts}),
        )

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

    # ---- Content Inventory (parsed-content manifest) ----------------

    def get_run_content_inventory(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> ContentInventoryDTO:
        """Project the run's `parsed_content_manifest` artifact into
        a normalized DTO the FE consumes for the Content Inventory
        tab.

        Reads ALL parsed-content manifest artifacts produced by this
        run and aggregates them — typically there's one per document,
        but a multi-document run combines them so the FE can show
        a single "what did the parser find?" view.

        When no manifest artifact exists (legacy runs, runs that
        haven't reached the manifest-emit step yet, runs that failed
        during compile), returns a `status="unavailable"` payload
        with the same operator-readable reason the availability
        resolver uses — single source of truth for the empty-state
        copy."""
        from j1.processing.manifest import ParsedContentManifest
        from j1.processing.results import ARTIFACT_KIND_PARSED_CONTENT_MANIFEST

        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        manifest_artifacts = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_PARSED_CONTENT_MANIFEST
        ]

        if not manifest_artifacts:
            from j1.ingestion_review.availability import (
                _parsed_content_reason,
            )
            return ContentInventoryDTO(
                run_id=run_id,
                document_id=None,
                document_name=run.metadata.get("document_name"),
                status="unavailable",
                unavailable_reason=_parsed_content_reason(run),
            )

        # Read each manifest payload from disk + aggregate. Most runs
        # have one manifest per document; we sum the stats and union
        # the items lists. Reads are JSON file reads, cheap.
        path_resolver = self._artifact_path_resolver(ctx)
        manifests: list[ParsedContentManifest] = []
        first_artifact_id: str | None = None
        for artifact in manifest_artifacts:
            try:
                path = path_resolver(artifact)
            except Exception:  # noqa: BLE001
                continue
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning(
                    "parsed-content manifest %s unreadable: %s",
                    artifact.artifact_id, exc,
                )
                continue
            if not isinstance(payload, dict):
                continue
            if first_artifact_id is None:
                first_artifact_id = artifact.artifact_id
            manifests.append(ParsedContentManifest.from_dict(payload))

        if not manifests:
            from j1.ingestion_review.availability import (
                _parsed_content_reason,
            )
            return ContentInventoryDTO(
                run_id=run_id,
                document_name=run.metadata.get("document_name"),
                status="unavailable",
                unavailable_reason=_parsed_content_reason(run),
                raw_artifact_id=manifest_artifacts[0].artifact_id,
            )

        # Aggregate. Single-document runs hit the trivial path.
        first = manifests[0]
        summary = ContentInventorySummaryDTO(
            page_count=_sum_optional(m.stats.page_count for m in manifests),
            text_block_count=sum(m.stats.text_blocks for m in manifests),
            table_count=sum(m.stats.tables for m in manifests),
            image_count=sum(m.stats.images for m in manifests),
            formula_count=sum(m.stats.equations for m in manifests),
            heading_count=_sum_optional(
                # `headings` field doesn't exist on ParsedContentStats
                # today — left as None until a producer surfaces it.
                None for _ in manifests
            ),
            other_count=0,
            total_items=sum(m.stats.total_items for m in manifests),
        )
        # Build the items list from every manifest. Producers cap
        # their own item lists; we surface them all so the FE can
        # filter / paginate client-side.
        items: list[ContentInventoryItemDTO] = []
        for manifest in manifests:
            for entry in manifest.items:
                items.append(ContentInventoryItemDTO(
                    item_id=entry.item_id,
                    type=entry.type,
                    page=entry.page_idx,
                    location=entry.source_path,
                    preview=entry.text_preview,
                    confidence=None,  # not on the per-element model today
                    passed_to_enrichment=None,
                    skipped=False,
                    skip_reason=None,
                    metadata=dict(entry.metadata),
                ))

        # `status` reflects whether the parser found anything, not
        # whether the run itself succeeded. A SUCCEEDED run with an
        # empty document still gets `status="empty"` — distinct from
        # `"unavailable"` (no manifest at all).
        if summary.total_items == 0:
            status = "empty"
        else:
            status = "completed"

        return ContentInventoryDTO(
            run_id=run_id,
            document_id=first.document_id or None,
            document_name=run.metadata.get("document_name"),
            status=status,
            source=ContentInventorySourceDTO(
                compiler="raganything" if first.parser == "raganything" else None,
                parser=first.parser or None,
                parser_version=first.parser_version,
                parse_method=first.parse_method,
                profile=first.profile,
            ),
            summary=summary,
            items=items,
            raw_artifact_id=first_artifact_id,
        )

    # ---- Planning Report --------------------------------------------

    def get_run_planning(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> PlanningResultDTO:
        """Build the Planning Report response for the given run.

        Resolution order:
          1. **`planning_result` artifact (preferred).** When the
             post-compile planning activity ran, it persists the full
             Processing Plan as an artifact. We project that into the
             DTO and surface every section the FE renders (Document
             Understanding, Content Report, Quality Report, Execution
             Plan, rule-based comparison).
          2. **Audit-log fallback.** Older runs (pre-Phase 2 of
             post-compile planning, or runs where the activity was
             disabled / failed) only have `plan.generated` events in
             the audit log. We project those into the same DTO with
             `source="audit_log"`.
          3. **Unavailable.** Neither source yielded a plan — return
             `status="unavailable"` + an operator-readable reason.

        Raises `ReviewNotFound` when the run doesn't exist in the
        caller's tenant/project."""
        run = self._load_run(ctx, run_id)

        # 1. Try the post-compile planning_result artifact first.
        artifact_dto = self._read_planning_artifact_dto(ctx, run, run_id)
        if artifact_dto is not None:
            return artifact_dto

        # 2. Audit-log fallback.
        plan_payload, plan_action = self._read_latest_plan_payload(ctx, run_id)
        if plan_payload is None:
            from j1.ingestion_review.availability import _planning_reason
            return PlanningResultDTO(
                run_id=run_id,
                document_id=run.document_id or None,
                document_name=run.metadata.get("document_name"),
                status="unavailable",
                unavailable_reason=_planning_reason(run),
                llm_recommendation=PlanningLLMRecommendationDTO(
                    status="disabled",
                ),
            )
        plan_dict = plan_payload.get("plan") or {}

        # Decisions: project each PlannedStep into the DTO shape.
        decisions: list[PlanningStepDecisionDTO] = []
        for step in plan_dict.get("steps") or []:
            if not isinstance(step, dict):
                continue
            decisions.append(PlanningStepDecisionDTO(
                step_id=str(step.get("step_id") or step.get("name") or ""),
                stage=str(step.get("stage") or ""),
                decision=str(step.get("decision") or "RUN"),
                enabled=bool(step.get("enabled", True)),
                required=bool(step.get("required") or False),
                source=str(step.get("source") or "default"),
                reason=step.get("reason"),
                risk_level=str(step.get("risk_level") or "low"),
                estimated_cost_tier=str(step.get("estimated_cost_tier") or "NONE"),
                llm_class=str(step.get("llm_class") or "none"),
                expected_engine=step.get("expected_engine"),
                expected_provider=step.get("expected_provider"),
                dependency_step_ids=list(step.get("dependency_step_ids") or []),
                warning=step.get("warning"),
                metadata=dict(step.get("metadata") or {}),
            ))

        assessment = PlanningAssessmentDTO(
            mode=str(plan_dict.get("mode") or ""),
            policy=str(plan_dict.get("policy") or ""),
            confidence=float(plan_dict.get("confidence") or 0.0),
            estimated_cost_level=str(plan_dict.get("estimated_cost_level") or "low"),
            fast_llm_used=bool(plan_dict.get("fast_llm_used") or False),
            requires_vision=bool(plan_dict.get("requires_vision") or False),
            requires_premium_llm=bool(plan_dict.get("requires_premium_llm") or False),
            reasons=_collect_plan_reasons(plan_dict),
            warnings=[
                str(w) for w in (plan_dict.get("warnings") or [])
                if w
            ],
        )

        digest = self._build_content_digest(ctx, run)
        llm_rec = self._build_llm_recommendation(plan_dict)

        return PlanningResultDTO(
            run_id=run_id,
            document_id=str(plan_dict.get("document_id") or run.document_id or "") or None,
            document_name=run.metadata.get("document_name"),
            status="completed",
            generated_at=plan_payload.get("occurred_at") or plan_payload.get("generated_at"),
            revised=plan_action == ACTION_PROGRESS_PLAN_REVISED,
            source="audit_log",
            planning_phase="initial",
            assessment=assessment,
            decisions=decisions,
            digest=digest,
            llm_recommendation=llm_rec,
        )

    def get_run_enrichment_result(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> dict:
        """Return the Wave-6 typed enrichment overlay for `run_id`.

        Reads the `enrichment_result` artifact (persisted after the
        post-compile enrichment stage runs). Same envelope shape as
        the other overlay endpoints: `status / runId / plan /
        artifactId` (`plan` carries the
        `EnrichmentResult.to_payload()` dict here)."""
        from j1.processing.results import ARTIFACT_KIND_ENRICHMENT_RESULT

        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        candidates = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_ENRICHMENT_RESULT
        ]
        base = {
            "runId": run_id,
            "documentId": run.document_id or None,
            "documentName": run.metadata.get("document_name"),
        }
        if not candidates:
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "No enrichment result was persisted for this run "
                    "yet. Enrichment may have been skipped by policy, "
                    "the run may predate the enrichment overlay, or "
                    "persistence failed."
                ),
                "plan": None,
            }
        candidates.sort(key=lambda a: a.updated_at, reverse=True)
        artifact = candidates[0]
        path_resolver = self._artifact_path_resolver(ctx)
        try:
            path = path_resolver(artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ReviewNotFound):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "enrichment_result artifact exists but could not "
                    "be read; check workspace permissions."
                ),
                "plan": None,
            }
        if not isinstance(payload, dict):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "enrichment_result artifact has an unexpected "
                    "shape (not a JSON object)."
                ),
                "plan": None,
            }
        return {
            **base,
            "status": "completed",
            "unavailableReason": None,
            "artifactId": artifact.artifact_id,
            "plan": payload,
        }

    def get_run_compile_result(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> dict:
        """Return the typed normalized compile result for `run_id`.

        Reads the `compile_result_summary` artifact (persisted by
        `_persist_compile_result_summary` after compile + retry loop
        completes). Mirrors `get_run_initial_execution_plan` /
        `get_run_enrich_plan` envelope shape; `plan` is the
        `NormalizedCompileResult.to_payload()` dict.

        Returns `status="unavailable"` with a reason when the
        artifact wasn't persisted (legacy run, compile failed before
        the persist step, persistence failed)."""
        from j1.processing.results import ARTIFACT_KIND_COMPILE_RESULT_SUMMARY

        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        candidates = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_COMPILE_RESULT_SUMMARY
        ]
        base = {
            "runId": run_id,
            "documentId": run.document_id or None,
            "documentName": run.metadata.get("document_name"),
        }
        if not candidates:
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "No compile result summary was persisted for this "
                    "run yet. Compile may not have completed, the run "
                    "may predate the normalizer, or persistence failed."
                ),
                "plan": None,
            }
        candidates.sort(key=lambda a: a.updated_at, reverse=True)
        artifact = candidates[0]
        path_resolver = self._artifact_path_resolver(ctx)
        try:
            path = path_resolver(artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ReviewNotFound):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "compile_result_summary artifact exists but could "
                    "not be read; check workspace permissions."
                ),
                "plan": None,
            }
        if not isinstance(payload, dict):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "compile_result_summary artifact has an unexpected "
                    "shape (not a JSON object)."
                ),
                "plan": None,
            }
        return {
            **base,
            "status": "completed",
            "unavailableReason": None,
            "artifactId": artifact.artifact_id,
            "plan": payload,
        }

    def get_run_initial_execution_plan(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> dict:
        """Return the pre-compile initial execution plan for `run_id`.

        Reads the `initial_execution_plan` artifact (persisted by the
        workflow's pre-compile `build_initial_execution_plan` activity).
        When no artifact exists — e.g. legacy run, profiling failed,
        run hasn't reached the pre-compile build yet — the response
        carries `status="unavailable"` with an operator-readable
        reason. Schema is the `InitialExecutionPlan.to_payload()`
        dict, exposed under the `plan` field; the wrapping dict adds
        `runId` / `documentId` / `unavailableReason` for FE rendering.

        Raises `ReviewNotFound` when the run doesn't exist in the
        caller's tenant/project."""
        from j1.processing.results import ARTIFACT_KIND_INITIAL_EXECUTION_PLAN

        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        candidates = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_INITIAL_EXECUTION_PLAN
        ]
        base = {
            "runId": run_id,
            "documentId": run.document_id or None,
            "documentName": run.metadata.get("document_name"),
        }
        if not candidates:
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "No initial execution plan was persisted for this "
                    "run yet. The run may predate the pre-compile "
                    "planner, profiling may have failed, or persistence "
                    "failed."
                ),
                "plan": None,
            }
        candidates.sort(key=lambda a: a.updated_at, reverse=True)
        artifact = candidates[0]
        path_resolver = self._artifact_path_resolver(ctx)
        try:
            path = path_resolver(artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ReviewNotFound):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "initial_execution_plan artifact exists but could "
                    "not be read; check workspace permissions."
                ),
                "plan": None,
            }
        if not isinstance(payload, dict):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "initial_execution_plan artifact has an unexpected "
                    "shape (not a JSON object)."
                ),
                "plan": None,
            }
        return {
            **base,
            "status": "completed",
            "unavailableReason": None,
            "artifactId": artifact.artifact_id,
            "plan": payload,
        }

    def get_run_enrich_plan(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> dict:
        """Return the post-compile rule-based enrich-plan for `run_id`.

        Reads the `post_compile_enrich_plan` artifact (persisted by
        the workflow's `_run_post_compile_enrich_assessment` step).
        When no artifact exists — e.g. legacy run, compile failed
        before assessment, run hasn't reached post-compile yet — the
        response carries `status="unavailable"` with an
        operator-readable reason. Schema is the
        `PostCompileEnrichPlan.to_payload()` dict, exposed under the
        `plan` field; the wrapping dict adds `runId` /
        `documentId` / `unavailableReason` for FE rendering.

        Raises `ReviewNotFound` when the run doesn't exist in the
        caller's tenant/project."""
        from j1.processing.results import ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN

        run = self._load_run(ctx, run_id)
        artifacts = self._resolve_run_artifacts(ctx, run)
        candidates = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN
        ]
        base = {
            "runId": run_id,
            "documentId": run.document_id or None,
            "documentName": run.metadata.get("document_name"),
        }
        if not candidates:
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "No post-compile enrich plan was persisted for this "
                    "run yet. Compile may not have completed, the run "
                    "predates the assessor, or persistence failed."
                ),
                "plan": None,
            }
        # Most-recent wins on replay duplicates.
        candidates.sort(key=lambda a: a.updated_at, reverse=True)
        artifact = candidates[0]
        path_resolver = self._artifact_path_resolver(ctx)
        try:
            path = path_resolver(artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ReviewNotFound):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "post_compile_enrich_plan artifact exists but could "
                    "not be read; check workspace permissions."
                ),
                "plan": None,
            }
        if not isinstance(payload, dict):
            return {
                **base,
                "status": "unavailable",
                "unavailableReason": (
                    "post_compile_enrich_plan artifact has an unexpected "
                    "shape (not a JSON object)."
                ),
                "plan": None,
            }
        return {
            **base,
            "status": "completed",
            "unavailableReason": None,
            "artifactId": artifact.artifact_id,
            "plan": payload,
        }

    def _read_planning_artifact_dto(
        self, ctx: ProjectContext, run, run_id: str,
    ) -> "PlanningResultDTO | None":
        """Return a `PlanningResultDTO` projected from the run's
        `planning_result` artifact, or None when no artifact exists.

        Production path for runs whose post-compile planning activity
        ran. The projector translates the artifact's persistent shape
        into the DTO in one place so the audit-log fallback and the
        artifact path don't drift.
        """
        from j1.processing.planning_result import PlanningResult
        from j1.processing.results import ARTIFACT_KIND_PLANNING_RESULT

        artifacts = self._resolve_run_artifacts(ctx, run)
        candidates = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_PLANNING_RESULT
        ]
        if not candidates:
            return None
        # Prefer the most recent one (by `updated_at`) — replays may
        # produce duplicates that share an artifact_id but differ in
        # mtime. A stable sort makes the projection deterministic.
        candidates.sort(key=lambda a: a.updated_at, reverse=True)
        artifact = candidates[0]
        path_resolver = self._artifact_path_resolver(ctx)
        try:
            path = path_resolver(artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ReviewNotFound):
            return None
        if not isinstance(payload, dict):
            return None

        result = PlanningResult.from_dict(payload)
        return _planning_artifact_to_dto(
            run_id=run_id,
            document_name=run.metadata.get("document_name"),
            result=result,
            artifact_id=artifact.artifact_id,
        )

    def _read_latest_plan_payload(
        self, ctx: ProjectContext, run_id: str,
    ) -> tuple[dict | None, str | None]:
        """Walk the audit log and return the most recent
        `plan.generated` / `plan.revised` payload for `run_id`.

        Single source of truth for "what plan does the FE render?".
        Mirrors the REST adapter's `_read_run_plan` but stays in the
        service so the projector can return a richer DTO without
        leaking parsing logic into the adapter."""
        path = self._workspace.audit(ctx) / AUDIT_LOG_FILENAME
        if not path.exists():
            return None, None
        latest_payload: dict | None = None
        latest_action: str | None = None
        latest_occurred_at: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("correlation_id") != run_id:
                continue
            action = data.get("action") or ""
            if action not in (
                ACTION_PROGRESS_PLAN_GENERATED,
                ACTION_PROGRESS_PLAN_REVISED,
            ):
                continue
            payload = data.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            occurred_at = data.get("occurred_at")
            # Last-write-wins by occurred_at; the audit log appends in
            # order so a string compare is safe (ISO-8601 ordering).
            if (
                latest_occurred_at is None
                or (occurred_at or "") >= latest_occurred_at
            ):
                latest_payload = {**payload, "occurred_at": occurred_at}
                latest_action = action
                latest_occurred_at = occurred_at or ""
        return latest_payload, latest_action

    def _planning_event_present(
        self, ctx: ProjectContext, run_id: str,
    ) -> bool:
        """Cheap existence check used by `summarize_run` to set
        `availableViews.planning`. Avoids reshaping the payload."""
        path = self._workspace.audit(ctx) / AUDIT_LOG_FILENAME
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("correlation_id") != run_id:
                continue
            if data.get("action") in (
                ACTION_PROGRESS_PLAN_GENERATED,
                ACTION_PROGRESS_PLAN_REVISED,
            ):
                return True
        return False

    def _build_content_digest(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> PlanningContentDigestDTO | None:
        """Build the lightweight content digest from the parsed-content
        manifest artifact, capped by the deployment's planning settings.

        Returns None when no manifest exists yet — typical when the
        Planning Report is consumed mid-run, before compile finishes.
        The DTO's `digest=None` lets the FE render the rule-based
        assessment without a digest panel."""
        from j1.processing.manifest import ParsedContentManifest
        from j1.processing.results import (
            ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
        )

        artifacts = self._resolve_run_artifacts(ctx, run)
        manifest_artifacts = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_PARSED_CONTENT_MANIFEST
        ]
        if not manifest_artifacts:
            return None

        max_blocks = self._planning_settings.max_sample_blocks
        max_chars = self._planning_settings.max_preview_chars

        path_resolver = self._artifact_path_resolver(ctx)
        manifests: list[ParsedContentManifest] = []
        for artifact in manifest_artifacts:
            try:
                path = path_resolver(artifact)
            except Exception:  # noqa: BLE001
                continue
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                manifests.append(ParsedContentManifest.from_dict(payload))

        if not manifests:
            return None

        text_blocks = sum(m.stats.text_blocks for m in manifests)
        return PlanningContentDigestDTO(
            page_count=_sum_optional(m.stats.page_count for m in manifests),
            text_block_count=text_blocks,
            table_count=sum(m.stats.tables for m in manifests),
            image_count=sum(m.stats.images for m in manifests),
            formula_count=sum(m.stats.equations for m in manifests),
            heading_count=None,
            total_items=sum(m.stats.total_items for m in manifests),
            sampled_block_count=min(text_blocks, max_blocks),
            max_preview_chars=max_chars,
        )

    def _build_llm_recommendation(
        self, plan_dict: dict,
    ) -> PlanningLLMRecommendationDTO:
        """Assemble the LLM-recommendation block.

        Today this is a thin skeleton driven by:
          * The plan payload's `fast_llm_used` flag — if the rule-based
            planner already consulted a fast LLM hint, surface that.
          * The deployment's `J1_LLM_PLANNING_ENABLED` setting.

        Phase 2 will swap in a real LLM-assisted planner that produces
        a structured recommendation; the DTO shape is forwards
        compatible so adding the call doesn't break consumers."""
        if not self._planning_settings.llm_planning_enabled:
            return PlanningLLMRecommendationDTO(
                status="disabled",
                model_profile=self._planning_settings.model_profile,
            )
        if plan_dict.get("fast_llm_used"):
            return PlanningLLMRecommendationDTO(
                status="advisory",
                model_profile=self._planning_settings.model_profile,
                summary=(
                    "Rule-based planner consulted the fast LLM role "
                    "for an advisory hint."
                ),
            )
        return PlanningLLMRecommendationDTO(
            status="advisory",
            model_profile=self._planning_settings.model_profile,
            summary=(
                "LLM-assisted planning is enabled but the planner did "
                "not invoke the model for this document."
            ),
        )

    # ---- Operational actions -----------------------------------------

    def delete_run(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Soft-delete an ingestion run.

        Tombstones the `IngestionRun` record (status=DELETED) and
        every `ArtifactRecord` belonging to the run (sets
        `metadata.deleted_at`). Tombstoned records stay on disk for
        audit; `_resolve_run_artifacts` excludes them from every
        read path so the FE no longer surfaces them.

        Idempotent — calling twice produces the same response shape;
        the second call counts zero newly-tombstoned records.

        Returns a deletion-report dict the REST layer envelopes:
        `{run_id, status, tombstoned_artifact_count, was_already_deleted, deleted_at}`.

        Raises `ReviewNotFound` if the run doesn't exist.
        Raises `RunStillActive` (409 at the REST boundary) if the run
        is currently RUNNING — operators must `cancel` first."""
        from datetime import datetime, timezone
        from j1.runs.models import RunStatus

        run = self._load_run(ctx, run_id)
        # Guard: don't tombstone an in-flight run. The workflow could
        # still be writing artifacts; tombstoning mid-flight produces
        # confusing partial state.
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(run.status) in active_states:
            raise RunStillActive(
                f"run {run_id!r} is currently {run.status} — cancel it before deleting"
            )

        already_deleted = str(run.status) == RunStatus.DELETED.value
        deleted_at = datetime.now(timezone.utc).isoformat()
        tombstoned = 0

        # Tombstone every artifact tagged with this run_id, plus
        # lineage-resolved artifacts (so the FE's filter actually
        # hides them all). Skip records already tombstoned.
        artifacts = self._resolve_run_artifacts(ctx, run)
        # `_resolve_run_artifacts` already filters out deleted
        # records, so on a re-delete we get an empty list — that's
        # the idempotent path.
        for a in artifacts:
            existing_meta = dict(a.metadata or {})
            if existing_meta.get("deleted_at"):
                continue
            existing_meta["deleted_at"] = deleted_at
            existing_meta["deleted_by"] = actor
            # Re-register with updated metadata. The registry's
            # `update_metadata` is the right call; fall back to
            # `add` if the registry only supports add (some test
            # fixtures).
            update = getattr(self._artifacts, "update_metadata", None)
            if callable(update):
                try:
                    update(ctx, a.artifact_id, existing_meta)
                    tombstoned += 1
                    continue
                except Exception:  # noqa: BLE001
                    pass
            # Fallback: replace via add() — works on the in-memory
            # test fixture, no-ops on a real registry that rejects
            # duplicate ids.
            from dataclasses import replace as _replace
            try:
                self._artifacts.add(_replace(a, metadata=existing_meta))
                tombstoned += 1
            except Exception:  # noqa: BLE001 — best-effort tombstone
                continue

        # Flip the run record to DELETED last so the run still
        # resolves during the tombstone loop above.
        if not already_deleted:
            run.status = RunStatus.DELETED
            run.updated_at = datetime.now(timezone.utc)
            metadata = dict(run.metadata or {})
            metadata["deleted_at"] = deleted_at
            metadata["deleted_by"] = actor
            run.metadata = metadata
            self._run_store.upsert(ctx, run)

        return {
            "run_id": run_id,
            "status": RunStatus.DELETED.value,
            "tombstoned_artifact_count": tombstoned,
            "was_already_deleted": already_deleted,
            "deleted_at": deleted_at,
        }

    def resume_from_checkpoint(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        candidate_settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate that the prior run is resumable under the candidate
        settings and return the carry-forward plan.

        Pure validation — does NOT create a new run record or dispatch
        a workflow. The REST endpoint composes this with run-record
        creation and workflow dispatch (so the service stays
        store-agnostic and easy to test).

        `candidate_settings` is the proposed-new-run settings dict
        (same shape as `RESUME_SETTINGS_FIELDS`). When the prior run's
        snapshot hash matches, the returned dict carries:

            {
              "run_id": str,                 # the prior run id
              "snapshot": dict,              # the full resume_snapshot
              "resumable_steps": list[str],  # steps the new run can skip
              "carry_forward_artifact_ids": list[str],
              "carry_forward_artifact_kinds": list[str],
            }

        Raises:
          - `ReviewNotFound` if the run doesn't exist (404).
          - `RunStillActive` if the run is in an in-flight state (409).
          - `ResumeNotPossible` if the run is DELETED, terminated
            without a snapshot, or status is otherwise non-terminal
            in a way resume can't proceed (412).
          - `ResumeIncompatible` if `candidate_settings` doesn't match
            the prior settings hash (412 with structured diff).
        """
        from j1.ingestion_review.exceptions import (
            ResumeIncompatible, ResumeNotPossible,
        )
        from j1.runs.models import RunStatus
        from j1.runs.resume import (
            RESUMABLE_STAGES, compatible_settings, settings_diff,
        )

        run = self._load_run(ctx, run_id)
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(run.status) in active_states:
            raise RunStillActive(
                f"run {run_id!r} is currently {run.status} — "
                "cancel it before resuming"
            )
        if str(run.status) == RunStatus.DELETED.value:
            raise ResumeNotPossible(
                f"run {run_id!r} is deleted — cannot resume"
            )
        if not run.is_terminal():
            raise ResumeNotPossible(
                f"run {run_id!r} is in non-resumable state {run.status}"
            )
        snapshot = (
            (run.metadata or {}).get("resume_snapshot")
            if isinstance(run.metadata, dict)
            else None
        )
        if not isinstance(snapshot, dict) or "settings_snapshot" not in snapshot:
            raise ResumeNotPossible(
                f"run {run_id!r} has no resume snapshot — terminated "
                "before resume support landed, or via a path that "
                "doesn't snapshot (e.g. cancelled). Use full-reindex."
            )
        prior_settings = snapshot.get("settings_snapshot") or {}
        if not compatible_settings(prior_settings, candidate_settings):
            diff = settings_diff(prior_settings, candidate_settings)
            raise ResumeIncompatible(
                f"settings drifted since run {run_id!r} — refusing to "
                "resume; full-reindex instead",
                diff=diff,
            )
        completed = list(snapshot.get("completed_steps") or [])
        # Intersect with the policy-allowed set: only enrich + graph
        # are safe to skip in v1. Compile + chunks always re-run
        # because their outputs are the structural backbone.
        resumable = [s for s in completed if s in RESUMABLE_STAGES]
        carry_ids = list(snapshot.get("produced_artifact_ids") or [])
        carry_kinds = list(snapshot.get("produced_artifact_kinds") or [])
        return {
            "run_id": run_id,
            "snapshot": dict(snapshot),
            "resumable_steps": resumable,
            "carry_forward_artifact_ids": carry_ids,
            "carry_forward_artifact_kinds": carry_kinds,
        }

    def purge_run(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        actor: str = "system",
        require_already_deleted: bool = True,
    ) -> dict[str, Any]:
        """Hard-delete an ingestion run.

        Physically removes:
          1. Each artifact file on disk (via `Path.unlink`).
          2. Each artifact record in the registry (via
             `delete_by_artifact_id`).
          3. Every JSONL snapshot of the run record (via
             `IngestionRunStore.purge`).

        Audit-log events are PRESERVED — compliance requires the
        full history of what happened, even after the run record is
        gone. Validation sets / runs that reference this run_id are
        cascaded separately by the REST orchestration (the review
        service doesn't own those stores).

        `require_already_deleted=True` (default) refuses to operate
        on a run that hasn't been soft-deleted first. Two-step
        ritual reduces accidental data loss — the operator does
        DELETE → confirm → POST /purge. Set False to skip the gate
        when an operator explicitly invokes purge on an undeleted
        terminal run (admin tooling).

        Returns a report dict the REST layer envelopes:
          {
            "run_id": str,
            "artifacts_purged": int,        # records removed
            "files_deleted": int,            # files actually removed from disk
            "files_missing": int,            # already absent (idempotent path)
            "snapshots_removed": int,        # JSONL snapshots of run record
            "purged_at": str (ISO),
          }

        Raises:
          - `ReviewNotFound` if the run doesn't exist (404).
          - `RunStillActive` if the run is in an in-flight state (409).
          - `RunNotTerminal` if `require_already_deleted=True` and
            the run isn't already soft-deleted (409 — operator
            must `DELETE` first).
        """
        from j1.ingestion_review.exceptions import RunNotTerminal
        from j1.runs.models import RunStatus

        run = self._load_run(ctx, run_id)
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(run.status) in active_states:
            raise RunStillActive(
                f"run {run_id!r} is currently {run.status} — "
                "cancel it before purging"
            )
        if require_already_deleted and str(run.status) != RunStatus.DELETED.value:
            raise RunNotTerminal(
                f"run {run_id!r} is {run.status} — soft-delete it "
                "first (DELETE /ingestion-runs/{id}) before purging"
            )

        # Resolve EVERY artifact tagged with this run_id, including
        # the tombstoned ones (soft-delete sets metadata.deleted_at;
        # the resolver hides them by default — opt in here so the
        # purge sees the full set).
        artifacts = self._resolve_run_artifacts(
            ctx, run, include_deleted=True,
        )
        files_deleted = 0
        files_missing = 0
        artifacts_purged = 0
        for a in artifacts:
            # File deletion first — the registry record is the only
            # pointer to where the file lives. If we delete the
            # record before the file, a crash leaves an orphaned
            # file on disk with no way to find it.
            try:
                path = self._resolve_artifact_path(ctx, a)
            except Exception:  # noqa: BLE001 — unresolvable path; skip file
                path = None
            if path is not None:
                try:
                    if path.exists():
                        path.unlink()
                        files_deleted += 1
                    else:
                        files_missing += 1
                except OSError:
                    files_missing += 1
            # Registry record next.
            delete_fn = getattr(
                self._artifacts, "delete_by_artifact_id", None,
            )
            if callable(delete_fn):
                try:
                    if delete_fn(ctx, a.artifact_id):
                        artifacts_purged += 1
                except Exception:  # noqa: BLE001 — best-effort
                    pass
        # Run record last — once it's gone, the resolver can no
        # longer enumerate the artifacts (lineage is broken). Doing
        # this last keeps the purge restartable on partial failures.
        snapshots_removed = 0
        try:
            if self._run_store.purge(ctx, run_id):
                snapshots_removed = 1
        except Exception:  # noqa: BLE001 — defensive, store may not implement
            pass
        from datetime import datetime as _dt, timezone as _tz
        return {
            "run_id": run_id,
            "actor": actor,
            "artifacts_purged": artifacts_purged,
            "files_deleted": files_deleted,
            "files_missing": files_missing,
            "snapshots_removed": snapshots_removed,
            "purged_at": _dt.now(_tz.utc).isoformat(),
        }

    def rebuild_index_only(
        self,
        ctx: ProjectContext,
        run_id: str,
    ) -> dict[str, Any]:
        """Validate that the prior run has chunks the index can re-read
        and return the carry-forward chunk artifact IDs.

        Pure validation — does NOT create a new run record or dispatch
        a workflow. The REST endpoint composes this with run-record
        creation and workflow dispatch.

        Returns:
            {
              "run_id": str,                            # the prior run id
              "chunk_artifact_ids": list[str],          # carry-forward IDs
              "chunk_artifact_kinds": list[str],        # parallel kinds
              "indexer_kind": str | None,               # the prior run's indexer
            }

        Raises:
          - `ReviewNotFound` if the run doesn't exist (404).
          - `RunStillActive` if the run is in an in-flight state (409).
          - `ResumeNotPossible` if the run is DELETED, never produced
            chunks, or has no resume snapshot for the carry-forward
            (412).
        """
        from j1.ingestion_review.exceptions import ResumeNotPossible
        from j1.runs.models import RunStatus

        run = self._load_run(ctx, run_id)
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(run.status) in active_states:
            raise RunStillActive(
                f"run {run_id!r} is currently {run.status} — "
                "cancel it before rebuilding the index"
            )
        if str(run.status) == RunStatus.DELETED.value:
            raise ResumeNotPossible(
                f"run {run_id!r} is deleted — cannot rebuild index"
            )
        # Snapshot is the canonical source of carry-forward IDs (it's
        # what the workflow already promised to persist at terminal).
        # Falling back to walking the artifact registry would work
        # but introduces a second source of truth — keep snapshot as
        # the only path so a missing snapshot fails closed.
        snapshot = (
            (run.metadata or {}).get("resume_snapshot")
            if isinstance(run.metadata, dict)
            else None
        )
        if not isinstance(snapshot, dict):
            raise ResumeNotPossible(
                f"run {run_id!r} has no resume snapshot — terminated "
                "before snapshot machinery landed, or via a path that "
                "doesn't snapshot (e.g. cancelled). Use full-reindex."
            )
        ids = list(snapshot.get("produced_artifact_ids") or [])
        kinds = list(snapshot.get("produced_artifact_kinds") or [])
        # Filter to the artifact kinds the index activity actually
        # consumes. Today the index reads `chunk` artifacts; passing
        # other kinds is harmless but wastes work — keep the carry
        # forward narrow so the index activity sees exactly what it
        # needs and the per-stage required-output rules don't trip.
        chunk_ids: list[str] = []
        chunk_kinds: list[str] = []
        for aid, akind in zip(ids, kinds):
            if akind == "chunk":
                chunk_ids.append(aid)
                chunk_kinds.append(akind)
        if not chunk_ids:
            raise ResumeNotPossible(
                f"run {run_id!r} produced no chunk artifacts — nothing "
                "to re-index. Use full-reindex to rebuild from source."
            )
        snap_settings = snapshot.get("settings_snapshot") or {}
        return {
            "run_id": run_id,
            "chunk_artifact_ids": chunk_ids,
            "chunk_artifact_kinds": chunk_kinds,
            "indexer_kind": snap_settings.get("indexer_kind"),
        }

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
        *,
        include_deleted: bool = False,
    ) -> list[ArtifactRecord]:
        """Return artifacts produced by `run`.

        Two strategies, applied in order:

          1. **Direct tag** — match `record.metadata["run_id"] == run.run_id`.
             Fast path for runs whose artifacts were tagged at registration
             time (Phase 4).

          2. **Lineage fallback (transitive)** — start from artifacts whose
             `source_document_ids` overlaps the run's target document set
             (typically the compile-stage outputs), then iteratively pull in
             any artifact whose `source_artifact_ids` overlaps the
             accumulating set. The iteration is required because downstream
             stages (graph_json, enriched.*) record `source_artifact_ids`
             pointing at compile artifacts — they carry NO
             `source_document_ids`, so a single-hop check leaves them
             unresolved. Without this walk the Graph tab silently disables
             on legacy untagged runs even though graph_json artifacts
             exist on disk.

        `include_deleted=True` opts into seeing tombstoned records.
        Used by the hard-delete (purge) path which needs to physically
        remove the same files soft-delete tombstoned. Every other
        caller wants the default (False) — soft-deleted artifacts
        stay invisible to read surfaces."""
        all_artifacts = self._artifacts.list_artifacts(ctx)
        # Soft-deleted artifact filter: any record carrying
        # `metadata.deleted_at` is hidden from every read path. The
        # tombstone stays on disk for audit; only the listing surface
        # excludes it. The purge path opts back in via
        # `include_deleted=True` so it can physically delete the
        # tombstoned files + records.
        if not include_deleted:
            all_artifacts = [
                a for a in all_artifacts
                if not (
                    isinstance(getattr(a, "metadata", None), dict)
                    and a.metadata.get("deleted_at")
                )
            ]

        target_doc_ids = set(_document_ids(run))

        tagged: list[ArtifactRecord] = []
        lineage_candidates: list[ArtifactRecord] = []
        for record in all_artifacts:
            tagged_run_id = record.metadata.get("run_id")
            if tagged_run_id == run.run_id:
                tagged.append(record)
                continue
            if tagged_run_id is not None and tagged_run_id != run.run_id:
                # Tagged for a different run — never include via lineage.
                continue
            lineage_candidates.append(record)

        if tagged:
            return tagged

        # Transitive lineage walk over the untagged candidates only.
        # Step 1: seed with artifacts whose source documents match the run.
        # Step 2: iteratively pull in artifacts whose source_artifact_ids
        # overlap the seed (or any subsequently included artifact).
        if not target_doc_ids:
            return []
        seed_ids: set[str] = set()
        included: list[ArtifactRecord] = []
        for record in lineage_candidates:
            if target_doc_ids.intersection(record.source_document_ids):
                seed_ids.add(record.artifact_id)
                included.append(record)

        # Fixed-point: keep walking until no new artifact gets pulled in.
        # Bounded by the candidate count, so worst-case is O(N²) which is
        # fine at the artifact-registry scales we run (hundreds, not
        # millions).
        added = True
        while added:
            added = False
            for record in lineage_candidates:
                if record.artifact_id in seed_ids:
                    continue
                if seed_ids.intersection(record.source_artifact_ids):
                    seed_ids.add(record.artifact_id)
                    included.append(record)
                    added = True
        return included

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

    # `low_confidence_count` counts WARNING-severity progress events
    # that flagged a confidence concern. Lightweight projection — the
    # full confidence-finding count requires reading the
    # `enriched.confidence_assessment` artifacts (the Quality tab's
    # projector does that). For the Overview scorecard the warning
    # count is the right scale-of-attention indicator.
    low_confidence_count = sum(
        1 for w in warnings
        if w.severity in ("warning", "error")
        and (
            "confidence" in (w.code or "").lower()
            or "confidence" in (w.message or "").lower()
        )
    )

    return QualitySummaryDTO(
        overall_confidence=overall_value,
        warning_count=len(warnings),
        low_confidence_count=low_confidence_count,
    )


def _planning_artifact_to_dto(
    *,
    run_id: str,
    document_name: str | None,
    result,
    artifact_id: str,
) -> PlanningResultDTO:
    """Project a `PlanningResult` artifact into the wire DTO.

    Surfaces every section the FE Planning Report renders:
      * `assessment` — same compact summary the audit-log path emits
        so downstream code (Planning Report tab, badges) can read one
        DTO regardless of source.
      * `decisions` — projected from `execution_plan.steps` so the FE
        renders one consistent table.
      * `document_understanding`, `content_report`, `quality_report`,
        `execution_plan`, `rule_based_assessment`,
        `rule_based_comparison` — surfaced verbatim as dicts; the FE
        knows the shape.

    Designed so older bundles that only consume `assessment` /
    `decisions` keep working — the rich fields live on top, optional.
    """
    plan = dict(result.execution_plan or {})
    # Defensive: `steps` is contractually a dict keyed by step name.
    # An LLM-emitted plan occasionally returns a list of step
    # objects; treating it as a list crashes the downstream `.get()`
    # / `.items()` calls with
    # `AttributeError: 'list' object has no attribute 'get'` —
    # observed mid-run as the BUILD_CONTENT_INVENTORY-stage failure.
    # Coerce list-shaped input by keying off `name` / `step_id`;
    # leave empty when the shape is unrecognised.
    raw_steps = plan.get("steps")
    if isinstance(raw_steps, dict):
        steps = raw_steps
    elif isinstance(raw_steps, list):
        steps = {}
        for entry in raw_steps:
            if not isinstance(entry, dict):
                continue
            key = (
                entry.get("name")
                or entry.get("step_id")
                or entry.get("id")
            )
            if key:
                steps[str(key)] = entry
    else:
        steps = {}

    # `decisions` for legacy FE: map each post-compile step into the
    # PlanningStepDecisionDTO shape (with `decision=RUN/SKIP`).
    decisions: list[PlanningStepDecisionDTO] = []
    for step_name, entry in steps.items():
        if step_name == "chunking":
            continue
        if not isinstance(entry, dict):
            continue
        enabled = bool(entry.get("enabled"))
        decisions.append(PlanningStepDecisionDTO(
            step_id=step_name,
            stage=step_name.upper(),
            decision="RUN" if enabled else "SKIP",
            enabled=enabled,
            required=bool(entry.get("required", False)),
            source=str(result.source or "rule_based"),
            reason=entry.get("reason"),
            risk_level="low",
            estimated_cost_tier="NONE",
            llm_class=str(entry.get("model_profile") or "none"),
            dependency_step_ids=[],
            metadata={"scope": entry.get("scope"), "pages": entry.get("pages") or []},
        ))

    # `assessment` summary mirrors the audit-log path so the FE's
    # scorecards work without branching on source.
    understanding = result.document_understanding or {}
    bias = (understanding.get("recommended_analysis_bias") or {}) if isinstance(
        understanding, dict
    ) else {}
    assessment = PlanningAssessmentDTO(
        mode=str(plan.get("estimated_time") or ""),
        policy=str(result.recommended_profile or ""),
        confidence=float(result.confidence or 0.0),
        estimated_cost_level=str(plan.get("estimated_cost") or "low"),
        fast_llm_used=result.source == "llm",
        requires_vision=bool(
            (steps.get("vision_enrichment") or {}).get("enabled"),
        ),
        requires_premium_llm=str(result.recommended_profile) == "premium",
        reasons=list(
            (result.decision_summary or {}).get("main_reasoning") or []
        ),
        warnings=list(result.warnings or []),
    )

    # LLM recommendation: status follows `source`.
    if result.source == "llm":
        llm_status = "applied"
    elif result.source == "rule_based_fallback":
        llm_status = "failed"
    else:
        llm_status = "disabled"
    llm_rec = PlanningLLMRecommendationDTO(
        status=llm_status,
        summary=(result.decision_summary or {}).get("overall_assessment"),
        failure_reason=next(
            (
                w for w in (result.warnings or [])
                if "fallback" in str(w).lower()
                or "unavailable" in str(w).lower()
            ),
            None,
        ) if llm_status == "failed" else None,
    )

    return PlanningResultDTO(
        run_id=run_id,
        document_id=result.document_id or None,
        document_name=document_name,
        status="completed",
        generated_at=result.created_at,
        revised=False,
        source=result.source,
        planning_phase=result.planning_phase,
        assessment=assessment,
        decisions=decisions,
        digest=None,  # rich digest stays inside the planning context;
                      # the artifact does not duplicate it. The FE has
                      # the full execution plan instead.
        llm_recommendation=llm_rec,
        document_understanding=dict(understanding) if understanding else None,
        decision_summary=dict(result.decision_summary or {}) or None,
        content_report=dict(result.content_report or {}) or None,
        quality_report=dict(result.quality_report or {}) or None,
        execution_plan=plan or None,
        rule_based_assessment=dict(result.rule_based_assessment or {}) or None,
        rule_based_comparison=dict(result.rule_based_comparison or {}) or None,
        next_actions=list(result.next_actions or []),
        warnings=list(result.warnings or []),
        raw_artifact_id=artifact_id,
        domain_context=(
            dict(result.domain_context) if result.domain_context else None
        ),
        planner_mode=getattr(result, "planner_mode", None) or result.source,
    )


def _collect_plan_reasons(plan_dict: dict) -> list[str]:
    """Pull per-step reasons + plan-level warnings into a single
    operator-readable list for the Planning Report's assessment block.

    Dedupes preserving order — a planner that names the same reason
    on two steps (e.g. 'mode text_only does not include enrichment'
    on enrich + graph) is normalised to one entry."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for step in plan_dict.get("steps") or []:
        if not isinstance(step, dict):
            continue
        reason = step.get("reason")
        if not reason:
            continue
        text = str(reason).strip()
        if text and text not in seen_set:
            seen_set.add(text)
            seen.append(text)
    return seen


def _sum_optional(values) -> int | None:
    """Sum a stream of `int | None`. Returns None when EVERY value
    is None (e.g. no producer surfaced page_count); otherwise sums
    the populated entries. Used by the Content Inventory aggregator
    so a missing per-document signal doesn't zero out the aggregate
    silently."""
    materialized = [v for v in values if v is not None]
    if not materialized:
        return None
    return sum(materialized)


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
