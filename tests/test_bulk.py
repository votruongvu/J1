"""Tests for j1.integration.bulk primitives.

Covers:
- Export rows for documents / sources / chunks (artifacts) / citations /
  metadata / feedback are valid NDJSON and round-trip through the
  Pydantic schemas.
- Import accepts valid lines and returns succeeded counts.
- Import rejects invalid JSON, schema-violating rows, and
  cross-tenant lines — each shows up in `failures` with the right
  error code and a 1-based line number.
- Re-importing the same checksum is counted as `skipped_idempotent`,
  not a duplicate failure.
- `verify_metadata` returns INTEGRITY_MISMATCH when stored fields don't
  match the supplied projection.
- `verify_metadata` returns DOCUMENT_NOT_FOUND for unknown documents.
"""

import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.integration import (
    ArtifactExportRecord,
    BulkExportService,
    BulkImportFailureRecord,
    BulkImportResult,
    BulkImportService,
    CitationExportRecord,
    DocumentExportRecord,
    FeedbackExportRecord,
    JsonlFeedbackStore,
    MetadataExportRecord,
    SourceExportRecord,
)
from j1.integration.bulk.result import (
    ERROR_CODE_DOCUMENT_NOT_FOUND,
    ERROR_CODE_INTEGRITY_MISMATCH,
    ERROR_CODE_INVALID_JSON,
    ERROR_CODE_PROJECT_MISMATCH,
    ERROR_CODE_SCHEMA,
)
from j1.integration.feedback import FeedbackRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


def _now() -> datetime:
    return datetime(2026, 5, 3, 8, 0, 0, tzinfo=timezone.utc)


# ---- Fixtures --------------------------------------------------------


@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


@pytest.fixture
def export_service(registry, artifact_registry, feedback_store) -> BulkExportService:
    return BulkExportService(registry, artifact_registry, feedback_store)


@pytest.fixture
def import_service(registry) -> BulkImportService:
    return BulkImportService(registry)


def _seed_document(ctx, registry, *, document_id="doc-1",
                   checksum="sha256:doc-1", file_size=10) -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=file_size,
        checksum=checksum,
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


def _seed_artifact(ctx, registry, *, artifact_id="a-1") -> ArtifactRecord:
    record = ArtifactRecord(
        artifact_id=artifact_id, project=ctx, kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}", byte_size=20,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_now(), updated_at=_now(),
        source_document_ids=["doc-1"],
        metadata={"source_location": "page 4"},
    )
    registry.add(record)
    return record


def _parse_ndjson(byte_chunks) -> list[dict]:
    """Drain a byte iterator and parse each line as JSON."""
    body = b"".join(byte_chunks).decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ---- Export ----------------------------------------------------------


def test_export_documents_yields_one_line_per_record(
    export_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1")
    _seed_document(ctx, registry, document_id="doc-2", checksum="sha256:doc-2")
    rows = _parse_ndjson(export_service.export_documents(ctx))
    assert {r["documentId"] for r in rows} == {"doc-1", "doc-2"}
    # Round-trips through the Pydantic schema
    for row in rows:
        DocumentExportRecord.model_validate(row)


def test_export_sources_emits_same_shape_as_documents(
    export_service, ctx, registry,
):
    _seed_document(ctx, registry)
    docs = _parse_ndjson(export_service.export_documents(ctx))
    sources = _parse_ndjson(export_service.export_sources(ctx))
    assert docs == sources  # alias


def test_export_chunks_round_trips_artifact_records(
    export_service, ctx, artifact_registry,
):
    _seed_artifact(ctx, artifact_registry, artifact_id="a-1")
    rows = _parse_ndjson(export_service.export_artifacts(ctx))
    assert len(rows) == 1
    ArtifactExportRecord.model_validate(rows[0])
    assert rows[0]["artifactId"] == "a-1"
    assert rows[0]["sourceDocumentIds"] == ["doc-1"]


def test_export_citations_one_row_per_artifact_source_pair(
    export_service, ctx, artifact_registry,
):
    a1 = _seed_artifact(ctx, artifact_registry, artifact_id="a-1")
    # Mutate the seeded artifact's source_document_ids by re-adding with
    # multiple source ids.
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-2", project=ctx, kind=a1.kind,
        location="compiled/a-2.txt", content_hash="sha256:a-2",
        byte_size=10, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_now(), updated_at=_now(),
        source_document_ids=["doc-A", "doc-B"],
    ))
    rows = _parse_ndjson(export_service.export_citations(ctx))
    # a-1 → doc-1 ; a-2 → doc-A, doc-B
    triples = {(r["artifactId"], r["sourceDocumentId"]) for r in rows}
    assert triples == {("a-1", "doc-1"), ("a-2", "doc-A"), ("a-2", "doc-B")}
    for row in rows:
        CitationExportRecord.model_validate(row)


def test_export_metadata_projection(export_service, ctx, registry):
    _seed_document(ctx, registry)
    rows = _parse_ndjson(export_service.export_metadata(ctx))
    assert len(rows) == 1
    MetadataExportRecord.model_validate(rows[0])
    # Field projection: not the full DocumentExportRecord — no
    # storedFilename
    assert "storedFilename" not in rows[0]


def test_export_feedback_round_trips(
    export_service, ctx, feedback_store,
):
    feedback_store.add(FeedbackRecord(
        feedback_id="fb-1", project=ctx,
        target_kind="artifact", target_id="a-1",
        submitted_at=_now(), rating=1, comment="good", actor="alice",
        correlation_id="run-1",
    ))
    rows = _parse_ndjson(export_service.export_feedback(ctx))
    assert len(rows) == 1
    FeedbackExportRecord.model_validate(rows[0])
    assert rows[0]["feedbackId"] == "fb-1"
    assert rows[0]["actor"] == "alice"


def test_export_emits_one_record_per_line_no_embedded_newlines(
    export_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1")
    body = b"".join(export_service.export_documents(ctx)).decode("utf-8")
    # Every line is parseable JSON on its own — no spurious newlines
    # inside records.
    for line in body.splitlines():
        assert line  # non-empty
        json.loads(line)


# ---- Import: success path -------------------------------------------


def _doc_line(**overrides) -> bytes:
    base = {
        "documentId": "doc-X",
        "tenantId": "acme",
        "projectId": "alpha",
        "originalFilename": "x.pdf",
        "storedFilename": "doc-X.pdf",
        "mimeType": "application/pdf",
        "fileSize": 1024,
        "checksum": "sha256:doc-X",
        "status": "pending",
        "createdAt": "2026-05-03T08:00:00+00:00",
    }
    base.update(overrides)
    return (json.dumps(base) + "\n").encode("utf-8")


def test_import_documents_accepts_valid_rows(import_service, ctx, registry):
    lines = [_doc_line(documentId="d-1", checksum="sha256:1"),
             _doc_line(documentId="d-2", checksum="sha256:2")]
    result = import_service.import_documents(ctx, lines)
    assert result.succeeded == 2
    assert result.skipped_idempotent == 0
    assert result.failures == []
    # Verify the new docs appear via the registry
    ids = {d.document_id for d in registry.list_documents(ctx)}
    assert {"d-1", "d-2"}.issubset(ids)


def test_import_skips_blank_lines_and_comments(import_service, ctx, registry):
    body = b"\n  \n" + _doc_line(checksum="sha256:dx") + b"\n\n"
    result = import_service.import_documents(ctx, body.splitlines())
    assert result.succeeded == 1
    assert result.failures == []


# ---- Import: idempotency --------------------------------------------


def test_import_skips_existing_checksum_as_idempotent(
    import_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1", checksum="sha256:dup")
    line = _doc_line(documentId="other-id", checksum="sha256:dup")
    result = import_service.import_documents(ctx, [line])
    assert result.succeeded == 0
    assert result.skipped_idempotent == 1
    assert result.failures == []


def test_import_round_trip_is_idempotent(
    import_service, export_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1", checksum="sha256:rt-1")
    _seed_document(ctx, registry, document_id="doc-2", checksum="sha256:rt-2")
    exported = list(export_service.export_documents(ctx))
    # Re-import → all rows skipped
    result = import_service.import_documents(ctx, exported)
    assert result.succeeded == 0
    assert result.skipped_idempotent == 2


# ---- Import: failure reporting --------------------------------------


def test_import_returns_invalid_json_failure_with_line_number(
    import_service, ctx,
):
    lines = [
        _doc_line(documentId="d-1", checksum="sha256:1"),
        b"{not valid json",
        _doc_line(documentId="d-2", checksum="sha256:2"),
    ]
    result = import_service.import_documents(ctx, lines)
    assert result.succeeded == 2
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.line_number == 2
    assert failure.code == ERROR_CODE_INVALID_JSON
    assert failure.record_id is None


def test_import_returns_schema_failure_with_record_id(import_service, ctx):
    bad = json.dumps({
        "documentId": "doc-bad",
        "tenantId": "acme",
        "projectId": "alpha",
        # missing originalFilename, storedFilename, etc.
    }).encode("utf-8") + b"\n"
    result = import_service.import_documents(ctx, [bad])
    assert result.succeeded == 0
    assert len(result.failures) == 1
    f = result.failures[0]
    assert f.code == ERROR_CODE_SCHEMA
    assert f.record_id == "doc-bad"
    assert f.line_number == 1


def test_import_rejects_cross_tenant_rows(import_service, ctx):
    line = _doc_line(documentId="evil", tenantId="other-tenant", projectId="alpha")
    result = import_service.import_documents(ctx, [line])
    assert result.succeeded == 0
    assert len(result.failures) == 1
    assert result.failures[0].code == ERROR_CODE_PROJECT_MISMATCH
    assert result.failures[0].record_id == "evil"


def test_import_rejects_cross_project_rows(import_service, ctx):
    line = _doc_line(documentId="x", tenantId="acme", projectId="other-project")
    result = import_service.import_documents(ctx, [line])
    assert result.failures[0].code == ERROR_CODE_PROJECT_MISMATCH


def test_import_partial_failure_continues_processing(import_service, ctx, registry):
    lines = [
        _doc_line(documentId="d-1", checksum="sha256:p-1"),  # ok
        b"{not json",                                          # invalid
        _doc_line(documentId="d-2", checksum="sha256:p-2",
                  status="bogus_status"),                     # bad enum
        _doc_line(documentId="d-3", checksum="sha256:p-3"),  # ok
    ]
    result = import_service.import_documents(ctx, lines)
    assert result.succeeded == 2  # d-1 + d-3
    assert len(result.failures) == 2
    codes = {f.code for f in result.failures}
    assert codes == {ERROR_CODE_INVALID_JSON, ERROR_CODE_SCHEMA}


# ---- Sources: alias of documents ------------------------------------


def test_import_sources_uses_same_shape(import_service, ctx, registry):
    line = _doc_line(documentId="src-1", checksum="sha256:src-1")
    result = import_service.import_sources(ctx, [line])
    assert result.succeeded == 1


# ---- Metadata: round-trip integrity verification -------------------


def _meta_line(**overrides) -> bytes:
    base = {
        "documentId": "doc-1",
        "tenantId": "acme",
        "projectId": "alpha",
        "originalFilename": "doc-1.pdf",
        "mimeType": "application/pdf",
        "fileSize": 10,
        "checksum": "sha256:doc-1",
        "status": "pending",
        "createdAt": "2026-05-03T08:00:00+00:00",
    }
    base.update(overrides)
    return (json.dumps(base) + "\n").encode("utf-8")


def test_verify_metadata_succeeds_when_fields_match(
    import_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1",
                   checksum="sha256:doc-1", file_size=10)
    result = import_service.verify_metadata(ctx, [_meta_line()])
    assert result.succeeded == 1
    assert result.failures == []


def test_verify_metadata_reports_missing_document(import_service, ctx, registry):
    # Don't seed — document doesn't exist
    result = import_service.verify_metadata(ctx, [_meta_line(documentId="missing")])
    assert result.succeeded == 0
    assert len(result.failures) == 1
    assert result.failures[0].code == ERROR_CODE_DOCUMENT_NOT_FOUND
    assert result.failures[0].record_id == "missing"


def test_verify_metadata_reports_field_mismatch(
    import_service, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1",
                   checksum="sha256:doc-1", file_size=10)
    bad = _meta_line(fileSize=99999)  # mismatch on file_size
    result = import_service.verify_metadata(ctx, [bad])
    assert result.succeeded == 0
    assert len(result.failures) == 1
    f = result.failures[0]
    assert f.code == ERROR_CODE_INTEGRITY_MISMATCH
    assert "fileSize" in f.message


def test_verify_metadata_blocks_cross_tenant(import_service, ctx, registry):
    _seed_document(ctx, registry)
    result = import_service.verify_metadata(ctx, [_meta_line(tenantId="other")])
    assert result.failures[0].code == ERROR_CODE_PROJECT_MISMATCH


# ---- BulkImportResult helpers ---------------------------------------


def test_bulk_import_result_total_and_has_failures():
    result = BulkImportResult(
        succeeded=2, skipped_idempotent=1,
        failures=[BulkImportFailureRecord(1, None, "X", "y")],
    )
    assert result.total == 4
    assert result.has_failures is True


def test_bulk_import_result_no_failures_default():
    assert BulkImportResult().has_failures is False
    assert BulkImportResult().total == 0
