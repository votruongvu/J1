from typing import BinaryIO, Protocol

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
