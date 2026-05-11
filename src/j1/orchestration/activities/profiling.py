"""Activity surface for adaptive ingestion planning.

Document profiling involves file I/O (`os.stat`, optional `pypdf.PdfReader`)
which Temporal workflows can't do directly — workflows must be
deterministic and side-effect-free. The planner itself is pure logic
and runs inside the workflow once it has a `DocumentProfile`."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from j1.intake.registry import SourceRegistry
from j1.orchestration.activities.payloads import ProjectScope
from j1.orchestration.errors import ERROR_TYPE_LOOKUP_FAILED
from j1.processing.profiling import DocumentProfile, DocumentProfiler
from j1.workspace.resolver import WorkspaceResolver

ACTIVITY_PROFILE_DOCUMENT = "j1.ingestion.profile_document"


@dataclass(frozen=True)
class ProfileDocumentInput:
    scope: ProjectScope
    document_id: str
    actor: str = "system"
    correlation_id: str | None = None


class ProfilingActivities:
    """Bundle of profile-time activities. Kept separate from
 `ProcessingActivities` so a deployment that doesn't enable adaptive
 planning doesn't have to register the extra dependency on
 `WorkspaceResolver` for the planning surface."""

    def __init__(
        self,
        sources: SourceRegistry,
        workspace: WorkspaceResolver,
        profiler: DocumentProfiler,
    ) -> None:
        self._sources = sources
        self._workspace = workspace
        self._profiler = profiler

    def all_activities(self) -> list:
        return [self.profile_document]

    @activity.defn(name=ACTIVITY_PROFILE_DOCUMENT)
    def profile_document(
        self, input: ProfileDocumentInput,
    ) -> DocumentProfile:
        """Resolve the document's on-disk path and hand it to the
 configured profiler. Returns a `DocumentProfile` that the
 workflow then feeds to the planner.

 Document-not-found is non-retryable (caller bug). Profiler
 errors are non-retryable too — pypdf/etc. failures are
 deterministic with respect to the input bytes."""
        ctx = input.scope.to_context()
        try:
            record = self._sources.get(ctx, input.document_id)
        except Exception as exc:
            raise ApplicationError(
                f"document {input.document_id!r} not found: {exc}",
                type=ERROR_TYPE_LOOKUP_FAILED,
                non_retryable=True,
            ) from exc

        raw_dir = self._workspace.raw(ctx)
        source_path = raw_dir / record.stored_filename
        try:
            return self._profiler.profile(input.document_id, str(source_path))
        except FileNotFoundError as exc:
            raise ApplicationError(
                f"source file for document {input.document_id!r} missing: {exc}",
                type=ERROR_TYPE_LOOKUP_FAILED,
                non_retryable=True,
            ) from exc
