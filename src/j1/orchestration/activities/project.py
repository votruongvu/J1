import json
from decimal import Decimal

from temporalio import activity

from j1.audit.recorder import AuditRecorder
from j1.cost.sink import COST_LOG_FILENAME
from j1.intake.registry import SourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.errors.exceptions import DocumentNotFoundError
from j1.orchestration.activities.payloads import (
    FinalizeInput,
    ProjectScope,
    SetDocumentStatusInput,
    SpendSummary,
    ValidateContextResult,
)
from j1.workspace.resolver import WorkspaceResolver

ACTIVITY_VALIDATE_CONTEXT = "j1.project.validate_context"
ACTIVITY_LIST_PENDING_DOCUMENTS = "j1.project.list_pending_documents"
ACTIVITY_COMPUTE_SPEND = "j1.project.compute_spend"
ACTIVITY_FINALIZE = "j1.project.finalize"
ACTIVITY_SET_DOCUMENT_STATUS = "j1.project.set_document_status"

ACTION_FINALIZED = "project.processing.finalized"
TARGET_PROJECT = "project"


class ProjectActivities:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        sources: SourceRegistry,
        audit: AuditRecorder,
    ) -> None:
        self._workspace = workspace
        self._sources = sources
        self._audit = audit

    def all_activities(self) -> list:
        return [
            self.validate_context,
            self.list_pending_documents,
            self.set_document_status,
            self.compute_spend,
            self.finalize,
        ]

    @activity.defn(name=ACTIVITY_VALIDATE_CONTEXT)
    def validate_context(self, scope: ProjectScope) -> ValidateContextResult:
        try:
            scope.to_context()
        except Exception as exc:
            return ValidateContextResult(valid=False, message=str(exc))
        return ValidateContextResult(valid=True)

    @activity.defn(name=ACTIVITY_LIST_PENDING_DOCUMENTS)
    def list_pending_documents(self, scope: ProjectScope) -> list[str]:
        ctx = scope.to_context()
        return [
            d.document_id
            for d in self._sources.list_documents(ctx)
            if d.status == ProcessingStatus.PENDING
        ]

    @activity.defn(name=ACTIVITY_SET_DOCUMENT_STATUS)
    def set_document_status(self, input: SetDocumentStatusInput) -> None:
        """Flip a document's registry status.

        Called by the workflow after each document is processed so a
        subsequent project-wide job doesn't re-pick it. Best-effort:
        an unknown status string or missing document is logged-and-
        ignored rather than raised — telemetry never blocks the
        workflow."""
        try:
            status = ProcessingStatus(input.status)
        except ValueError:
            activity.logger.warning(
                "set_document_status: ignoring unknown status %r for %s",
                input.status, input.document_id,
            )
            return
        ctx = input.scope.to_context()
        try:
            self._sources.update_status(ctx, input.document_id, status)
        except DocumentNotFoundError:
            activity.logger.warning(
                "set_document_status: document %s not in registry; "
                "skipping status update", input.document_id,
            )

    @activity.defn(name=ACTIVITY_COMPUTE_SPEND)
    def compute_spend(self, scope: ProjectScope) -> SpendSummary:
        ctx = scope.to_context()
        path = self._workspace.audit(ctx) / COST_LOG_FILENAME
        if not path.exists():
            return SpendSummary(total_amount="0", currency="USD", event_count=0)
        total = Decimal("0")
        currency = "USD"
        count = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            total += Decimal(data["amount"])
            currency = data.get("currency", currency)
            count += 1
        return SpendSummary(
            total_amount=str(total), currency=currency, event_count=count
        )

    @activity.defn(name=ACTIVITY_FINALIZE)
    def finalize(self, input: FinalizeInput) -> None:
        ctx = input.scope.to_context()
        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_FINALIZED,
            target_kind=TARGET_PROJECT,
            target_id=ctx.project_id,
            correlation_id=input.correlation_id,
            payload={
                "state": input.state,
                "artifact_count": len(input.artifact_ids),
                "artifact_ids": list(input.artifact_ids),
                "error": input.error,
            },
        )
