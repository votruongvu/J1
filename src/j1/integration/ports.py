from typing import BinaryIO, Protocol

from j1.integration.dto import (
    AnswerDTO,
    AnswerRequestDTO,
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
from j1.projects.context import ProjectContext


class DocumentIngestionPort(Protocol):
    """Accept a document into a project workspace."""

    def register_document(
        self,
        ctx: ProjectContext,
        content: BinaryIO,
        *,
        original_filename: str,
        mime_type: str | None = None,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> DocumentDTO: ...


class JobStatusPort(Protocol):
    """Look up a processing job's current status."""

    def get_job_status(
        self, ctx: ProjectContext, job_id: str
    ) -> JobStatusDTO: ...


class SearchPort(Protocol):
    """Keyword search against indexed project artifacts."""

    def search(
        self,
        ctx: ProjectContext,
        query: str,
        *,
        artifact_types: list[str] | None = None,
        max_results: int = 20,
    ) -> list[SearchHitDTO]: ...


class RetrievalPort(Protocol):
    """Resolve an artifact reference to its metadata view."""

    def get_artifact(
        self, ctx: ProjectContext, artifact_id: str
    ) -> ArtifactDTO: ...

    def list_artifacts(
        self,
        ctx: ProjectContext,
        *,
        kind: str | None = None,
    ) -> list[ArtifactDTO]: ...


class AnswerPort(Protocol):
    """Generate an answer for a project-scoped question."""

    def answer(
        self,
        ctx: ProjectContext,
        request: AnswerRequestDTO,
    ) -> AnswerDTO: ...


class CitationLookupPort(Protocol):
    """Return the source citations an artifact descends from."""

    def get_citations(
        self,
        ctx: ProjectContext,
        artifact_id: str,
    ) -> list[CitationDTO]: ...


class SourceLookupPort(Protocol):
    """Resolve a source document by ID."""

    def get_source(
        self, ctx: ProjectContext, document_id: str
    ) -> DocumentDTO: ...


class FeedbackPort(Protocol):
    """Capture user feedback against an artifact, query, or document."""

    def submit_feedback(
        self,
        ctx: ProjectContext,
        feedback: FeedbackDTO,
    ) -> FeedbackResultDTO: ...


class EventPublisherPort(Protocol):
    """Publish a domain event to the audit log."""

    def publish_event(
        self,
        ctx: ProjectContext,
        event: EventDTO,
    ) -> EventResultDTO: ...


class ProjectAdminPort(Protocol):
    """Project lifecycle: provision a workspace for a new project."""

    def create_project(
        self,
        tenant_id: str,
        request: ProjectCreateRequestDTO,
    ) -> ProjectDTO: ...


class JobControlPort(Protocol):
    """Start and signal long-running ingestion jobs."""

    async def start_project_job(
        self,
        ctx: ProjectContext,
        request: ProjectIngestionRequestDTO,
    ) -> JobActionResultDTO: ...

    async def pause_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO: ...

    async def resume_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO: ...

    async def cancel_job(
        self, ctx: ProjectContext, job_id: str
    ) -> JobActionResultDTO: ...


class CostSummaryPort(Protocol):
    """Aggregate spend for a project, optionally scoped by correlation."""

    def get_cost_summary(
        self,
        ctx: ProjectContext,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        query_id: str | None = None,
    ) -> CostSummaryDTO: ...


class ReviewPort(Protocol):
    """Manage the human-review queue."""

    def list_reviews(
        self,
        ctx: ProjectContext,
        *,
        pending_only: bool = True,
    ) -> list[ReviewItemDTO]: ...

    def apply_decision(
        self,
        ctx: ProjectContext,
        review_item_id: str,
        request: ReviewDecisionRequestDTO,
    ) -> ReviewDecisionResultDTO: ...
