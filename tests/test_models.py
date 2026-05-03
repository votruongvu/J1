from datetime import datetime, timezone
from decimal import Decimal

from j1.artifacts.models import ArtifactRecord
from j1.audit.events import AuditEvent
from j1.cost.events import CostEvent
from j1.documents.models import DocumentRecord, SourceDocument
from j1.jobs.models import JobRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_processing_status_values():
    assert {s.value for s in ProcessingStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }


def test_review_status_values():
    assert {s.value for s in ReviewStatus} == {
        "not_required",
        "pending",
        "approved",
        "rejected",
        "changes_requested",
    }


def test_source_document_construction():
    sd = SourceDocument(uri="file:///tmp/a.pdf", content_type="application/pdf")
    assert sd.uri == "file:///tmp/a.pdf"
    assert sd.metadata == {}


def test_document_record_construction():
    record = DocumentRecord(
        document_id="d1",
        project=_ctx(),
        uri="file:///tmp/a.pdf",
        content_hash="sha256:abc",
        byte_size=10,
        mime_type="application/pdf",
        status=ProcessingStatus.PENDING,
        created_at=_ts(),
        updated_at=_ts(),
    )
    assert record.document_id == "d1"
    assert record.status is ProcessingStatus.PENDING
    assert record.metadata == {}


def test_artifact_record_construction():
    record = ArtifactRecord(
        artifact_id="a1",
        project=_ctx(),
        kind="compiled.markdown",
        location="compiled/a1.md",
        content_hash="sha256:def",
        byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_ts(),
        updated_at=_ts(),
        source_document_ids=["d1"],
    )
    assert record.kind == "compiled.markdown"
    assert record.review_status is ReviewStatus.NOT_REQUIRED
    assert record.source_document_ids == ["d1"]


def test_job_record_construction():
    record = JobRecord(
        job_id="j1",
        project=_ctx(),
        kind="compile",
        status=ProcessingStatus.RUNNING,
        created_at=_ts(),
        updated_at=_ts(),
        attempt=1,
        correlation_id="corr-1",
    )
    assert record.attempt == 1
    assert record.status is ProcessingStatus.RUNNING


def test_audit_event_construction():
    event = AuditEvent(
        event_id="e1",
        occurred_at=_ts(),
        project=_ctx(),
        actor="system",
        action="document.ingested",
        target_kind="document",
        target_id="d1",
    )
    assert event.action == "document.ingested"
    assert event.payload == {}


def test_cost_event_construction():
    event = CostEvent(
        event_id="c1",
        occurred_at=_ts(),
        project=_ctx(),
        vendor="anthropic",
        model="claude-sonnet-4-6",
        unit_kind="input_tokens",
        units=1234,
        amount=Decimal("0.0123"),
        currency="USD",
    )
    assert event.amount == Decimal("0.0123")
    assert event.currency == "USD"
