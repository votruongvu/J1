from pathlib import Path

from temporalio import activity

from j1.audit.recorder import AuditRecorder
from j1.errors.exceptions import DuplicateDocumentError
from j1.intake.service import DocumentIntakeService
from j1.orchestration.activities.payloads import (
    DocumentRegistrationError,
    FinalizeProcessingInput,
    FinalizeProcessingResult,
    PrepareWorkspaceInput,
    PrepareWorkspaceResult,
    RegisterDocumentsInput,
    RegisterDocumentsResult,
    SkippedDocument,
    ValidateProjectInput,
    ValidateProjectResult,
)
from j1.workspace.resolver import WorkspaceResolver

ACTIVITY_VALIDATE_PROJECT = "j1.lifecycle.validate_project"
ACTIVITY_PREPARE_WORKSPACE = "j1.lifecycle.prepare_workspace"
ACTIVITY_REGISTER_DOCUMENTS = "j1.lifecycle.register_documents"
ACTIVITY_FINALIZE_PROCESSING = "j1.lifecycle.finalize_processing"

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"

ACTION_VALIDATED = "j1.lifecycle.project_validated"
ACTION_VALIDATION_FAILED = "j1.lifecycle.project_validation_failed"
ACTION_WORKSPACE_PREPARED = "j1.lifecycle.workspace_prepared"
ACTION_DOCUMENTS_REGISTERED = "j1.lifecycle.documents_registered"
ACTION_FINALIZED = "j1.lifecycle.processing_finalized"

TARGET_PROJECT = "project"


class ProjectLifecycleActivities:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        intake: DocumentIntakeService,
        audit: AuditRecorder,
    ) -> None:
        self._workspace = workspace
        self._intake = intake
        self._audit = audit

    def all_activities(self) -> list:
        return [
            self.validate_project_activity,
            self.prepare_workspace_activity,
            self.register_documents_activity,
            self.finalize_processing_activity,
        ]

    @activity.defn(name=ACTIVITY_VALIDATE_PROJECT)
    def validate_project_activity(
        self, input: ValidateProjectInput
    ) -> ValidateProjectResult:
        try:
            ctx = input.scope.to_context()
        except Exception as exc:
            return ValidateProjectResult(
                status=STATUS_FAILED,
                message=str(exc),
                error_type=type(exc).__name__,
            )
        self._audit.record(
            ctx,
            actor="system",
            action=ACTION_VALIDATED,
            target_kind=TARGET_PROJECT,
            target_id=ctx.project_id,
            payload={"tenant_id": ctx.tenant_id},
        )
        return ValidateProjectResult(status=STATUS_SUCCEEDED)

    @activity.defn(name=ACTIVITY_PREPARE_WORKSPACE)
    def prepare_workspace_activity(
        self, input: PrepareWorkspaceInput
    ) -> PrepareWorkspaceResult:
        ctx = input.scope.to_context()
        self._workspace.ensure(ctx)
        self._audit.record(
            ctx,
            actor="system",
            action=ACTION_WORKSPACE_PREPARED,
            target_kind=TARGET_PROJECT,
            target_id=ctx.project_id,
            payload={"tenant_id": ctx.tenant_id},
        )
        return PrepareWorkspaceResult(status=STATUS_SUCCEEDED)

    @activity.defn(name=ACTIVITY_REGISTER_DOCUMENTS)
    def register_documents_activity(
        self, input: RegisterDocumentsInput
    ) -> RegisterDocumentsResult:
        ctx = input.scope.to_context()
        registered: list[str] = []
        skipped: list[SkippedDocument] = []
        errors: list[DocumentRegistrationError] = []

        for source in input.documents:
            try:
                record = self._intake.register_from_path(
                    ctx,
                    Path(source.source_path),
                    original_filename=source.original_filename,
                    mime_type=source.mime_type,
                    actor=input.actor,
                    correlation_id=input.correlation_id,
                )
                registered.append(record.document_id)
            except DuplicateDocumentError as exc:
                skipped.append(
                    SkippedDocument(
                        source_path=source.source_path,
                        existing_document_id=exc.existing_document_id,
                    )
                )
            except Exception as exc:
                errors.append(
                    DocumentRegistrationError(
                        source_path=source.source_path,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                )

        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_DOCUMENTS_REGISTERED,
            target_kind=TARGET_PROJECT,
            target_id=ctx.project_id,
            correlation_id=input.correlation_id,
            payload={
                "registered_count": len(registered),
                "skipped_count": len(skipped),
                "error_count": len(errors),
            },
        )

        if errors and not registered and not skipped:
            status = STATUS_FAILED
        elif errors:
            status = STATUS_PARTIAL
        else:
            status = STATUS_SUCCEEDED

        return RegisterDocumentsResult(
            status=status,
            registered_document_ids=registered,
            skipped=skipped,
            errors=errors,
        )

    @activity.defn(name=ACTIVITY_FINALIZE_PROCESSING)
    def finalize_processing_activity(
        self, input: FinalizeProcessingInput
    ) -> FinalizeProcessingResult:
        ctx = input.scope.to_context()
        event_id = self._audit.record(
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
        return FinalizeProcessingResult(
            status=STATUS_SUCCEEDED, audit_event_id=event_id
        )
