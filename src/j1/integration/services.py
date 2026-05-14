import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.cost.aggregator import CostAggregator
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import (
    DocumentNotFoundError,
    InvalidIdentifierError,
)
from j1.integration.dto import (
    ArtifactDTO,
    CitationDTO,
    CostSummaryDTO,
    DocumentDTO,
    EventDTO,
    EventResultDTO,
    FeedbackDTO,
    FeedbackResultDTO,
    JobActionResultDTO,
    JobStatusDTO,
    ProjectCreateRequestDTO,
    ProjectDTO,
    ProjectIngestionRequestDTO,
    ReviewDecisionRequestDTO,
    ReviewDecisionResultDTO,
    ReviewItemDTO,
    SearchHitDTO,
)
from j1.integration.feedback import (
    FeedbackRecord,
    FeedbackStore,
)
from j1.intake.registry import SourceRegistry
from j1.intake.service import DocumentIntakeService
from j1.orchestration.activities.payloads import (
    ApplyReviewDecisionInput,
    ProjectScope,
)
from j1.orchestration.activities.review import ReviewActivities
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    SIGNAL_CANCEL,
    SIGNAL_PAUSE,
    SIGNAL_RESUME,
)
from j1.projects.context import ProjectContext
from j1.review.models import ReviewItem
from j1.review.queue import ReviewQueue
from j1.search.indexer import SearchHit
from j1.workspace.resolver import WorkspaceResolver


# ---- DTO converters -------------------------------------------------------


def _document_to_dto(record: DocumentRecord) -> DocumentDTO:
    return DocumentDTO(
        document_id=record.document_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        original_filename=record.original_filename,
        stored_filename=record.stored_filename,
        mime_type=record.mime_type,
        file_size=record.file_size,
        checksum=record.checksum,
        status=record.status.value,
        created_at=record.created_at,
        knowledge_state=record.knowledge_state,
        active_run_id=record.active_run_id,
        latest_version_id=record.latest_version_id,
    )


def _artifact_to_dto(record: ArtifactRecord) -> ArtifactDTO:
    return ArtifactDTO(
        artifact_id=record.artifact_id,
        tenant_id=record.project.tenant_id,
        project_id=record.project.project_id,
        kind=record.kind,
        location=record.location,
        content_hash=record.content_hash,
        byte_size=record.byte_size,
        status=record.status.value,
        review_status=record.review_status.value,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        source_document_ids=list(record.source_document_ids),
        source_artifact_ids=list(record.source_artifact_ids),
        metadata=dict(record.metadata),
    )


def _review_to_dto(item: ReviewItem) -> ReviewItemDTO:
    return ReviewItemDTO(
        review_item_id=item.review_item_id,
        tenant_id=item.project.tenant_id,
        project_id=item.project.project_id,
        target_kind=item.target_kind,
        target_id=item.target_id,
        review_status=item.review_status.value,
        requested_at=item.requested_at,
        actor=item.actor,
        notes=item.notes,
        metadata=dict(item.metadata),
    )


def _hit_to_dto(hit: SearchHit) -> SearchHitDTO:
    return SearchHitDTO(
        artifact_id=hit.artifact_id,
        artifact_type=hit.artifact_type,
        title=hit.title,
        score=hit.score,
        source_document_id=hit.source_document_id,
        source_location=hit.source_location,
        confidence=hit.confidence,
        review_status=hit.review_status,
        extracted_text=hit.extracted_text,
        chunk_id=hit.chunk_id,
        run_id=hit.run_id,
    )


def _evidence_hit_to_dto(hit) -> SearchHitDTO:
    """Phase 4 adapter-side hit → DTO. The adapter's ``EvidenceHit``
    carries the snapshot lineage; we surface it on the DTO so REST
    callers and the citation binder can verify the hit came from
    the active snapshot."""
    return SearchHitDTO(
        artifact_id=hit.artifact_id,
        artifact_type="evidence_chunk",
        title=(hit.content or "")[:80],
        score=hit.score,
        source_document_id=hit.document_id,
        source_location=None,
        confidence=0.0,
        review_status="not_required",
        extracted_text=hit.content or "",
        chunk_id=hit.chunk_id,
        run_id=hit.created_by_run_id,
        snapshot_id=hit.snapshot_id,
    )


# ---- Port implementations ------------------------------------------------


class DocumentIngestionService:
    def __init__(self, intake: DocumentIntakeService) -> None:
        self._intake = intake

    def register_document(
        self,
        ctx: ProjectContext,
        content: BinaryIO,
        *,
        original_filename: str,
        mime_type: str | None = None,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> DocumentDTO:
        record = self._intake.register_from_stream(
            ctx,
            content,
            original_filename=original_filename,
            mime_type=mime_type,
            actor=actor,
            correlation_id=correlation_id,
        )
        return _document_to_dto(record)


class TemporalJobStatusService:
    """Looks up a workflow's status by querying its `get_status` query.

 `client_provider` is a callable that returns a Temporal client (sync or
 async) — kept as a callable so the integration layer doesn't import
 `temporalio.client.Client` directly.
 """

    def __init__(
        self,
        client_provider: Callable[[], Any],
        *,
        status_query_name: str = "get_status",
    ) -> None:
        self._client_provider = client_provider
        self._status_query_name = status_query_name

    async def get_job_status(
        self, ctx: ProjectContext, job_id: str
    ) -> JobStatusDTO:
        client = self._client_provider()
        handle = client.get_workflow_handle(job_id)
        status = await handle.query(self._status_query_name)
        return JobStatusDTO(
            job_id=job_id,
            state=getattr(status, "state", "unknown"),
            current_operation=getattr(status, "current_operation", None),
            documents_total=int(getattr(status, "documents_total", 0)),
            documents_completed=int(getattr(status, "documents_completed", 0)),
            review_required=bool(getattr(status, "review_required", False)),
            budget_approval_required=bool(
                getattr(status, "budget_approval_required", False)
            ),
            error=getattr(status, "error", None),
        )


class SearchService:
    """Phase 4: the canonical operator-facing search surface.

    Old: wrapped ``SqliteSearchIndexer`` directly; queries hit the
    legacy FTS5 table, scoped by ``run_id`` columns.

    New: wraps an ``EvidenceIndexAdapter`` (Postgres FTS by default)
    and ALWAYS resolves the active-snapshot allowlist through the
    eligibility gate before issuing a query. A document is searchable
    iff it has ``active_snapshot_id`` set (Phase 3 retired the
    ``active_run_id`` fallback) and isn't detached/removed.

    Three modes of construction:

      * ``SearchService(adapter, registry)`` — the canonical
        snapshot-aware path. Production wiring.
      * ``SearchService(adapter)`` — adapter-only mode; the caller
        is responsible for passing ``allowed_snapshot_ids`` to
        ``search``. Used by the dev/debug REPL.
      * ``SearchService(indexer)`` — legacy SQLite path. Preserved
        ONLY for the bundled REST debug endpoint when no adapter is
        wired; never the strategic path. Phase 5 deletes this mode.
    """

    def __init__(
        self,
        adapter_or_indexer,
        registry=None,
    ) -> None:
        # Detect adapter vs legacy indexer by the presence of the
        # snapshot-aware ``search`` signature. The legacy indexer's
        # ``search`` takes ``artifact_types`` + ``max_results``; the
        # adapter's takes ``query`` + ``allowed_snapshot_ids`` +
        # ``max_results``. Cheap duck-type check at construction so
        # call sites don't have to pick the right constructor name.
        self._adapter = None
        self._indexer = None
        self._registry = registry
        from j1.search.evidence_adapter import PostgresFtsEvidenceAdapter
        if isinstance(adapter_or_indexer, PostgresFtsEvidenceAdapter):
            self._adapter = adapter_or_indexer
        else:
            # Phase 8 trace-only shim: the legacy SQLite path is
            # gone, but the SearchService constructor still accepts
            # a non-adapter object so legacy test wirings that pass
            # ``None`` or a custom mock don't crash at import time.
            # The ``search`` method raises on the legacy branch.
            self._indexer = adapter_or_indexer

    def search(
        self,
        ctx: ProjectContext,
        query: str,
        *,
        artifact_types: list[str] | None = None,
        max_results: int = 20,
    ) -> list[SearchHitDTO]:
        # Adapter path (Phase 4 canonical).
        if self._adapter is not None:
            allowlist = self._resolve_snapshot_allowlist(ctx)
            if not allowlist:
                # No visible snapshots → no results. Refuse to fall
                # through to the adapter without an allowlist (the
                # adapter would short-circuit anyway, but explicit
                # is better than implicit here).
                return []
            hits = self._adapter.search(
                ctx,
                query=query,
                allowed_snapshot_ids=allowlist,
                max_results=max_results,
            )
            return [_evidence_hit_to_dto(h) for h in hits]
        # Legacy SQLite path — Phase 5 removes.
        hits = self._indexer.search(
            ctx,
            query,
            artifact_types=artifact_types,
            max_results=max_results,
        )
        return [_hit_to_dto(h) for h in hits]

    def _resolve_snapshot_allowlist(self, ctx: ProjectContext) -> list[str]:
        """Pull the active-snapshot set from the source registry.
        Empty when no registry is wired (the caller didn't supply
        one) — operators wiring this without a registry are
        responsible for passing a snapshot allowlist explicitly."""
        if self._registry is None:
            return []
        from j1.query.eligibility import (
            resolve_eligible_active_snapshot_ids,
        )
        from j1.query.scope import WorkspaceScope
        result = resolve_eligible_active_snapshot_ids(
            ctx=ctx, scope=WorkspaceScope(), registry=self._registry,
        )
        return list(result.snapshot_ids)


class RetrievalService:
    def __init__(self, artifact_registry: ArtifactRegistry) -> None:
        self._artifacts = artifact_registry

    def get_artifact(
        self, ctx: ProjectContext, artifact_id: str
    ) -> ArtifactDTO:
        record = self._artifacts.get(ctx, artifact_id)
        return _artifact_to_dto(record)

    def list_artifacts(
        self,
        ctx: ProjectContext,
        *,
        kind: str | None = None,
    ) -> list[ArtifactDTO]:
        records = self._artifacts.list_artifacts(ctx, kind=kind)
        return [_artifact_to_dto(r) for r in records]


class CitationLookupService:
    """Returns the source documents an artifact's lineage points to."""

    def __init__(self, artifact_registry: ArtifactRegistry) -> None:
        self._artifacts = artifact_registry

    def get_citations(
        self, ctx: ProjectContext, artifact_id: str
    ) -> list[CitationDTO]:
        record = self._artifacts.get(ctx, artifact_id)
        citations: list[CitationDTO] = []
        for doc_id in record.source_document_ids:
            citations.append(
                CitationDTO(
                    artifact_id=record.artifact_id,
                    artifact_type=record.kind,
                    source_document_id=doc_id,
                    source_location=str(
                        record.metadata.get("source_location", "")
                    )
                    or None,
                )
            )
        if not citations:
            citations.append(
                CitationDTO(
                    artifact_id=record.artifact_id,
                    artifact_type=record.kind,
                    source_document_id=None,
                    source_location=None,
                )
            )
        return citations


class SourceLookupService:
    def __init__(self, sources: SourceRegistry) -> None:
        self._sources = sources

    def get_source(
        self, ctx: ProjectContext, document_id: str
    ) -> DocumentDTO:
        record = self._sources.get(ctx, document_id)
        return _document_to_dto(record)


class FeedbackService:
    def __init__(
        self,
        store: FeedbackStore,
        audit: AuditRecorder | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def submit_feedback(
        self,
        ctx: ProjectContext,
        feedback: FeedbackDTO,
    ) -> FeedbackResultDTO:
        feedback_id = self._id_factory()
        submitted_at = self._clock()
        record = FeedbackRecord(
            feedback_id=feedback_id,
            project=ctx,
            target_kind=feedback.target_kind,
            target_id=feedback.target_id,
            submitted_at=submitted_at,
            rating=feedback.rating,
            comment=feedback.comment,
            actor=feedback.actor,
            correlation_id=feedback.correlation_id,
            metadata=dict(feedback.metadata),
        )
        self._store.add(record)
        if self._audit is not None:
            self._audit.record(
                ctx,
                actor=feedback.actor or "system",
                action="j1.feedback.submitted",
                target_kind=feedback.target_kind,
                target_id=feedback.target_id,
                correlation_id=feedback.correlation_id,
                payload={
                    "feedback_id": feedback_id,
                    "rating": feedback.rating,
                    "comment": feedback.comment,
                },
            )
        return FeedbackResultDTO(
            feedback_id=feedback_id,
            submitted_at=submitted_at,
        )


class EventPublisherService:
    def __init__(self, audit: AuditRecorder) -> None:
        self._audit = audit

    def publish_event(
        self,
        ctx: ProjectContext,
        event: EventDTO,
    ) -> EventResultDTO:
        event_id = self._audit.record(
            ctx,
            actor=event.actor,
            action=event.action,
            target_kind=event.target_kind,
            target_id=event.target_id,
            payload=dict(event.payload),
            correlation_id=event.correlation_id,
        )
        return EventResultDTO(event_id=event_id)


# ---- Project / job-control services --------------------------------------


class ProjectAdminService:
    """Provisions per-project workspace directories."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def create_project(
        self,
        tenant_id: str,
        request: ProjectCreateRequestDTO,
    ) -> ProjectDTO:
        ctx = ProjectContext(
            tenant_id=tenant_id,
            project_id=request.project_id,
            profile=request.profile,
        )
        self._workspace.ensure(ctx)
        return ProjectDTO(
            project_id=ctx.project_id,
            tenant_id=ctx.tenant_id,
            profile=ctx.profile,
        )


class TemporalJobControlService:
    """Starts a `ProjectProcessingWorkflow` and signals it for control.

 `client_provider` returns a Temporal client (kept as a callable so the
 integration layer never imports `temporalio.client.Client` directly).

 `workflow_id_factory` lets callers supply deterministic id
 generation (e.g. `f"j1-{tenant}-{project}-{document_id}"`) so a
 repeated start for the same logical job collapses onto the same
 workflow id. The default uses a UUID suffix, which is appropriate
 for the bulk-job path (each invocation is intentionally a fresh
 run); deployments that drive per-document starts should pass a
 deterministic factory.

 `id_conflict_policy` controls Temporal's behaviour when a workflow
 with the requested id is already running. Default `None` keeps
 SDK behaviour (raise / fail). Pass
 `WorkflowIDConflictPolicy.USE_EXISTING` for paths where a
 duplicate trigger should return the in-flight handle instead of
 spawning a parallel run.
 """

    def __init__(
        self,
        client_provider: Callable[[], Any],
        *,
        task_queue: str,
        workflow_id_factory: Callable[[ProjectContext], str] | None = None,
        id_conflict_policy: Any = None,
    ) -> None:
        self._client_provider = client_provider
        self._task_queue = task_queue
        self._workflow_id_factory = workflow_id_factory or _default_workflow_id
        self._id_conflict_policy = id_conflict_policy

    async def start_project_job(
        self,
        ctx: ProjectContext,
        request: ProjectIngestionRequestDTO,
    ) -> JobActionResultDTO:
        client = self._client_provider()
        scope = ProjectScope.from_context(ctx)
        workflow_request = ProjectProcessingRequest(
            scope=scope,
            compiler_kind=request.compiler_kind,
            enricher_kind=request.enricher_kind,
            graph_builder_kind=request.graph_builder_kind,
            indexer_kind=request.indexer_kind,
            budget_limit_amount=request.budget_limit_amount,
            budget_currency=request.budget_currency,
            review_after=tuple(request.review_after),
            actor=request.actor,
            correlation_id=request.correlation_id,
        )
        workflow_id = self._workflow_id_factory(ctx)
        start_kwargs: dict[str, Any] = {
            "id": workflow_id,
            "task_queue": self._task_queue,
        }
        if self._id_conflict_policy is not None:
            start_kwargs["id_conflict_policy"] = self._id_conflict_policy
        await client.start_workflow(
            ProjectProcessingWorkflow.run,
            workflow_request,
            **start_kwargs,
        )
        return JobActionResultDTO(job_id=workflow_id, action="start")

    async def pause_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO:
        await self._signal(job_id, SIGNAL_PAUSE)
        return JobActionResultDTO(job_id=job_id, action="pause")

    async def resume_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO:
        await self._signal(job_id, SIGNAL_RESUME)
        return JobActionResultDTO(job_id=job_id, action="resume")

    async def cancel_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO:
        await self._signal(job_id, SIGNAL_CANCEL)
        return JobActionResultDTO(job_id=job_id, action="cancel")

    async def _signal(self, job_id: str, name: str) -> None:
        client = self._client_provider()
        handle = client.get_workflow_handle(job_id)
        await handle.signal(name)


def _default_workflow_id(ctx: ProjectContext) -> str:
    """Generate a fresh non-deterministic workflow id.

 Appropriate for the bulk-job path where each invocation is
 intentionally a separate run (operator clicks "run pipeline
 again"). Deployments that drive PER-DOCUMENT starts should pass
 a deterministic factory like `make_per_document_workflow_id`
 so a repeated trigger for the same logical document collapses
 onto the same workflow id.
 """
    return f"j1-{ctx.tenant_id}-{ctx.project_id}-{uuid.uuid4().hex[:12]}"


def make_per_document_workflow_id(
    ctx: ProjectContext, document_id: str,
) -> str:
    """Deterministic workflow id for per-document ingest triggers.

 Format: `j1-{tenant_id}-{project_id}-{document_id}`. Combined
 with `id_conflict_policy=USE_EXISTING` and intake's checksum
 dedup (which maps re-uploaded bytes back to the same
 `document_id`), this guarantees a single physical document is
 never processed by two parallel workflows."""
    return f"j1-{ctx.tenant_id}-{ctx.project_id}-{document_id}"


class CostSummaryService:
    def __init__(self, aggregator: CostAggregator) -> None:
        self._agg = aggregator

    def get_cost_summary(
        self,
        ctx: ProjectContext,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        query_id: str | None = None,
    ) -> CostSummaryDTO:
        total = self._agg.aggregate(
            ctx,
            correlation_id=correlation_id,
            document_id=document_id,
            query_id=query_id,
        )
        by_level = self._agg.by_levels(
            ctx,
            correlation_id=correlation_id,
            document_id=document_id,
            query_id=query_id,
        )
        return CostSummaryDTO(
            project_id=ctx.project_id,
            tenant_id=ctx.tenant_id,
            total_amount=str(total),
            by_level={
                level.value: str(amount) for level, amount in by_level.items()
            },
        )


class ReviewService:
    """Wraps the review queue and `apply_review_decision` activity."""

    def __init__(
        self,
        queue: ReviewQueue,
        review_activities: ReviewActivities,
    ) -> None:
        self._queue = queue
        self._review_activities = review_activities

    def list_reviews(
        self,
        ctx: ProjectContext,
        *,
        pending_only: bool = True,
    ) -> list[ReviewItemDTO]:
        items = (
            self._queue.list_pending(ctx)
            if pending_only
            else self._queue.list_items(ctx)
        )
        return [_review_to_dto(i) for i in items]

    def apply_decision(
        self,
        ctx: ProjectContext,
        review_item_id: str,
        request: ReviewDecisionRequestDTO,
    ) -> ReviewDecisionResultDTO:
        result = self._review_activities.apply_review_decision_activity(
            ApplyReviewDecisionInput(
                scope=ProjectScope.from_context(ctx),
                review_item_id=review_item_id,
                decision=request.decision,
                actor=request.actor,
                notes=request.notes,
                correlation_id=request.correlation_id,
            )
        )
        return ReviewDecisionResultDTO(
            review_item_id=result.review_item_id,
            review_status=result.review_status,
            audit_event_id=result.audit_event_id,
        )


# ---- Application facade ---------------------------------------------------


@dataclass(frozen=True)
class ApplicationFacade:
    """Bundle of port implementations — the surface adapters depend on.

 Adapters (REST, MCP, Webhook, etc.) take an `ApplicationFacade` and
 dispatch protocol-specific requests to the relevant port. They never
 reach into the underlying J1 services directly.

 Optional ports may be `None` if the deployment doesn't configure their
 backing service (no Temporal client, no FTS5 / profile loader, no review
 queue wiring). Adapters check for `None` and decline to expose those
 routes (typically with a 503).
 """

    ingestion: DocumentIngestionService
    retrieval: RetrievalService
    citation_lookup: CitationLookupService
    source_lookup: SourceLookupService
    feedback: FeedbackService
    event_publisher: EventPublisherService
    job_status: TemporalJobStatusService | None = None
    search: SearchService | None = None
    project_admin: ProjectAdminService | None = None
    job_control: TemporalJobControlService | None = None
    cost_summary: CostSummaryService | None = None
    review: ReviewService | None = None
