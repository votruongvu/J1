import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus
from j1.orchestration.activities.payloads import (
    FinalizeInput,
    ProjectScope,
)
from j1.orchestration.activities.project import (
    ACTIVITY_COMPUTE_SPEND,
    ACTIVITY_FINALIZE,
    ACTIVITY_LIST_PENDING_DOCUMENTS,
    ACTIVITY_VALIDATE_CONTEXT,
    ACTION_FINALIZED,
    ProjectActivities,
)


def _document(ctx, *, doc_id="doc-1", status=ProcessingStatus.PENDING) -> DocumentRecord:
    return DocumentRecord(
        document_id=doc_id,
        project=ctx,
        original_filename=f"{doc_id}.pdf",
        stored_filename=f"{doc_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{doc_id}",
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def project_activities(workspace, registry, audit_recorder) -> ProjectActivities:
    return ProjectActivities(
        workspace=workspace, sources=registry, audit=audit_recorder
    )


# Activity decorators


def test_activity_names(project_activities):
    names = [
        a.__temporal_activity_definition.name
        for a in project_activities.all_activities()
    ]
    assert ACTIVITY_VALIDATE_CONTEXT in names
    assert ACTIVITY_LIST_PENDING_DOCUMENTS in names
    assert ACTIVITY_COMPUTE_SPEND in names
    assert ACTIVITY_FINALIZE in names


# validate_context


def test_validate_context_accepts_valid_scope(project_activities):
    result = project_activities.validate_context(
        ProjectScope(tenant_id="acme", project_id="alpha")
    )
    assert result.valid is True
    assert result.message is None


def test_validate_context_rejects_bad_identifier(project_activities):
    result = project_activities.validate_context(
        ProjectScope(tenant_id="..", project_id="alpha")
    )
    assert result.valid is False
    assert result.message


# list_pending_documents


def test_list_pending_documents_empty(project_activities, ctx):
    assert (
        project_activities.list_pending_documents(ProjectScope.from_context(ctx))
        == []
    )


def test_list_pending_documents_filters_by_status(
    project_activities, registry, ctx
):
    registry.add(_document(ctx, doc_id="doc-1", status=ProcessingStatus.PENDING))
    registry.add(_document(ctx, doc_id="doc-2", status=ProcessingStatus.SUCCEEDED))
    registry.add(_document(ctx, doc_id="doc-3", status=ProcessingStatus.PENDING))
    pending = project_activities.list_pending_documents(
        ProjectScope.from_context(ctx)
    )
    assert sorted(pending) == ["doc-1", "doc-3"]


# compute_spend


def test_compute_spend_no_log(project_activities, ctx):
    summary = project_activities.compute_spend(ProjectScope.from_context(ctx))
    assert summary.total_amount == "0"
    assert summary.event_count == 0


def test_compute_spend_sums_log(project_activities, cost_recorder, ctx):
    from j1.cost.breakdown import CostBreakdown

    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=1,
            amount=Decimal("0.10"),
        ),
    )
    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=1,
            amount=Decimal("0.25"),
        ),
    )
    summary = project_activities.compute_spend(ProjectScope.from_context(ctx))
    assert Decimal(summary.total_amount) == Decimal("0.35")
    assert summary.event_count == 2
    assert summary.currency == "USD"


# finalize


def test_finalize_writes_audit_event(project_activities, workspace, ctx):
    project_activities.finalize(
        FinalizeInput(
            scope=ProjectScope.from_context(ctx),
            state="completed",
            artifact_ids=["a-1", "a-2"],
            correlation_id="run-1",
        )
    )
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["action"] == ACTION_FINALIZED
    assert parsed["target_kind"] == "project"
    assert parsed["target_id"] == "alpha"
    assert parsed["payload"]["state"] == "completed"
    assert parsed["payload"]["artifact_count"] == 2
    assert parsed["payload"]["artifact_ids"] == ["a-1", "a-2"]
    assert parsed["correlation_id"] == "run-1"


def test_finalize_includes_error_when_present(project_activities, workspace, ctx):
    project_activities.finalize(
        FinalizeInput(
            scope=ProjectScope.from_context(ctx),
            state="failed_final",
            error="budget rejected",
        )
    )
    parsed = json.loads(
        (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    )
    assert parsed["payload"]["error"] == "budget rejected"
