import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError
from j1.integration.dto import (
    AnswerDTO,
    AnswerRequestDTO,
    ArtifactDTO,
    CitationDTO,
    DocumentDTO,
    EventDTO,
    EventResultDTO,
    FeedbackDTO,
    FeedbackResultDTO,
    JobStatusDTO,
    SearchHitDTO,
)
from j1.integration.feedback import (
    FeedbackRecord,
    FeedbackStore,
)
from j1.intake.registry import SourceRegistry
from j1.intake.service import DocumentIntakeService
from j1.projects.context import ProjectContext
from j1.query.engine import HybridQueryEngine
from j1.query.models import QueryMode, QueryRequest
from j1.search.indexer import SearchHit, SqliteSearchIndexer


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
    def __init__(self, indexer: SqliteSearchIndexer) -> None:
        self._indexer = indexer

    def search(
        self,
        ctx: ProjectContext,
        query: str,
        *,
        artifact_types: list[str] | None = None,
        max_results: int = 20,
    ) -> list[SearchHitDTO]:
        hits = self._indexer.search(
            ctx,
            query,
            artifact_types=artifact_types,
            max_results=max_results,
        )
        return [_hit_to_dto(h) for h in hits]


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


class AnswerService:
    def __init__(self, query_engine: HybridQueryEngine) -> None:
        self._engine = query_engine

    def answer(
        self,
        ctx: ProjectContext,
        request: AnswerRequestDTO,
    ) -> AnswerDTO:
        try:
            mode = QueryMode(request.mode)
        except ValueError as exc:
            raise ValueError(f"unknown query mode: {request.mode!r}") from exc
        response = self._engine.query(
            ctx,
            QueryRequest(
                question=request.question,
                mode=mode,
                max_results=request.max_results,
                artifact_types=list(request.artifact_types),
            ),
        )
        return AnswerDTO(
            answer=response.answer,
            mode_used=response.mode_used,
            sources=[
                CitationDTO(
                    artifact_id=s.artifact_id,
                    artifact_type=s.artifact_type,
                    source_document_id=s.source_document_id,
                    source_location=s.source_location,
                )
                for s in response.sources
            ],
            related_artifacts=list(response.related_artifacts),
            confidence=response.confidence,
            confidence_level=response.confidence_level.value,
            review_required=response.review_required,
            warnings=list(response.warnings),
            warning_categories=[c.value for c in response.warning_categories],
        )


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


# ---- Application facade ---------------------------------------------------


@dataclass(frozen=True)
class ApplicationFacade:
    """Bundle of port implementations — the surface adapters depend on.

    Adapters (REST, MCP, Webhook, etc.) take an `ApplicationFacade` and
    dispatch protocol-specific requests to the relevant port. They never
    reach into the underlying J1 services directly.

    Optional ports (`job_status`, `search`, `answer`) can be `None` if the
    deployment doesn't configure their backing service (no Temporal client,
    no FTS5, no profile loader, etc.). Adapters check for `None` and
    decline to expose those routes.
    """

    ingestion: DocumentIngestionService
    retrieval: RetrievalService
    citation_lookup: CitationLookupService
    source_lookup: SourceLookupService
    feedback: FeedbackService
    event_publisher: EventPublisherService
    job_status: TemporalJobStatusService | None = None
    search: SearchService | None = None
    answer: AnswerService | None = None
