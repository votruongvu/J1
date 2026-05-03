import json
from pathlib import Path

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.orchestration.activities.lifecycle import (
    ACTIVITY_FINALIZE_PROCESSING,
    ACTIVITY_PREPARE_WORKSPACE,
    ACTIVITY_REGISTER_DOCUMENTS,
    ACTIVITY_VALIDATE_PROJECT,
)
from j1.orchestration.activities.payloads import (
    DocumentSource,
    FinalizeProcessingInput,
    PrepareWorkspaceInput,
    ProjectScope,
    RegisterDocumentsInput,
    ValidateProjectInput,
)
from j1.workspace.layout import WorkspaceArea


def _read_audit(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# Activity-defn metadata


def test_activity_names(lifecycle_activities):
    names = [
        a.__temporal_activity_definition.name
        for a in lifecycle_activities.all_activities()
    ]
    assert ACTIVITY_VALIDATE_PROJECT in names
    assert ACTIVITY_PREPARE_WORKSPACE in names
    assert ACTIVITY_REGISTER_DOCUMENTS in names
    assert ACTIVITY_FINALIZE_PROCESSING in names


# validate_project


def test_validate_project_succeeds(lifecycle_activities, ctx, workspace):
    result = lifecycle_activities.validate_project_activity(
        ValidateProjectInput(scope=ProjectScope.from_context(ctx))
    )
    assert result.status == "succeeded"
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "j1.lifecycle.project_validated"


def test_validate_project_returns_failed_for_bad_scope(lifecycle_activities):
    result = lifecycle_activities.validate_project_activity(
        ValidateProjectInput(scope=ProjectScope(tenant_id="..", project_id="x"))
    )
    assert result.status == "failed"
    assert result.error_type
    assert result.message


# prepare_workspace


def test_prepare_workspace_creates_full_layout(
    lifecycle_activities, ctx, workspace
):
    result = lifecycle_activities.prepare_workspace_activity(
        PrepareWorkspaceInput(scope=ProjectScope.from_context(ctx))
    )
    assert result.status == "succeeded"
    project_root = workspace.project_root(ctx)
    for area in WorkspaceArea:
        assert (project_root / area.value).is_dir()


def test_prepare_workspace_is_idempotent(lifecycle_activities, ctx):
    a = lifecycle_activities.prepare_workspace_activity(
        PrepareWorkspaceInput(scope=ProjectScope.from_context(ctx))
    )
    b = lifecycle_activities.prepare_workspace_activity(
        PrepareWorkspaceInput(scope=ProjectScope.from_context(ctx))
    )
    assert a.status == "succeeded"
    assert b.status == "succeeded"


# register_documents


def _stage(tmp_path: Path, name: str, content: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_register_documents_happy_path(lifecycle_activities, ctx, tmp_path):
    a = _stage(tmp_path, "a.txt", b"alpha")
    b = _stage(tmp_path, "b.txt", b"beta")
    result = lifecycle_activities.register_documents_activity(
        RegisterDocumentsInput(
            scope=ProjectScope.from_context(ctx),
            documents=[
                DocumentSource(source_path=str(a)),
                DocumentSource(source_path=str(b)),
            ],
        )
    )
    assert result.status == "succeeded"
    assert len(result.registered_document_ids) == 2
    assert result.skipped == []
    assert result.errors == []


def test_register_documents_dedupes_by_checksum(
    lifecycle_activities, ctx, tmp_path
):
    a = _stage(tmp_path, "a.txt", b"alpha")
    first = lifecycle_activities.register_documents_activity(
        RegisterDocumentsInput(
            scope=ProjectScope.from_context(ctx),
            documents=[DocumentSource(source_path=str(a))],
        )
    )
    second = lifecycle_activities.register_documents_activity(
        RegisterDocumentsInput(
            scope=ProjectScope.from_context(ctx),
            documents=[DocumentSource(source_path=str(a))],
        )
    )
    assert second.status == "succeeded"
    assert second.registered_document_ids == []
    assert len(second.skipped) == 1
    assert (
        second.skipped[0].existing_document_id
        == first.registered_document_ids[0]
    )


def test_register_documents_partial_on_error(
    lifecycle_activities, ctx, tmp_path
):
    a = _stage(tmp_path, "a.txt", b"alpha")
    bogus = tmp_path / "missing.txt"
    result = lifecycle_activities.register_documents_activity(
        RegisterDocumentsInput(
            scope=ProjectScope.from_context(ctx),
            documents=[
                DocumentSource(source_path=str(a)),
                DocumentSource(source_path=str(bogus)),
            ],
        )
    )
    assert result.status == "partial"
    assert len(result.registered_document_ids) == 1
    assert len(result.errors) == 1
    assert result.errors[0].source_path == str(bogus)


def test_register_documents_audits_summary(
    lifecycle_activities, ctx, tmp_path, workspace
):
    a = _stage(tmp_path, "a.txt", b"alpha")
    lifecycle_activities.register_documents_activity(
        RegisterDocumentsInput(
            scope=ProjectScope.from_context(ctx),
            documents=[DocumentSource(source_path=str(a))],
        )
    )
    summary_events = [
        e
        for e in _read_audit(workspace, ctx)
        if e["action"] == "j1.lifecycle.documents_registered"
    ]
    assert summary_events
    assert summary_events[0]["payload"]["registered_count"] == 1


# finalize_processing


def test_finalize_processing_writes_audit(lifecycle_activities, ctx, workspace):
    result = lifecycle_activities.finalize_processing_activity(
        FinalizeProcessingInput(
            scope=ProjectScope.from_context(ctx),
            state="completed",
            artifact_ids=["a", "b"],
            correlation_id="run-1",
        )
    )
    assert result.status == "succeeded"
    assert result.audit_event_id

    finalized = [
        e
        for e in _read_audit(workspace, ctx)
        if e["action"] == "j1.lifecycle.processing_finalized"
    ]
    assert finalized
    payload = finalized[0]["payload"]
    assert payload["state"] == "completed"
    assert payload["artifact_count"] == 2
    assert finalized[0]["correlation_id"] == "run-1"
