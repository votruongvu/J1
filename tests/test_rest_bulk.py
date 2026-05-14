"""End-to-end tests for the REST bulk import / export endpoints.

Verifies:
- GET /exports/{documents,sources,chunks,citations,metadata,feedback}.ndjson
 return Content-Type: application/x-ndjson with one JSON record per line.
- POST /imports/{documents,sources,metadata}.ndjson return the standard
 envelope with succeeded / skippedIdempotent / failures fields.
- Auth + scope rules apply identically (kb:read / kb:audit.read for
 exports; kb:ingest for imports).
- Idempotent re-import of an exported file does not double-write.
- Partial failure responses include the line number, error code, and
 recordId where available.
- /imports/metadata.ndjson is a no-op verifier (does not change the
 registry; reports INTEGRITY_MISMATCH when fields don't line up).
- Capability advertisement turns off when the services aren't wired.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.documents.models import DocumentRecord
from j1.integration import (
    ApiKeyAuthenticator,
    ApiKeyRecord,
    ApplicationFacade,
    BulkExportService,
    BulkImportService,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SCOPE_ANSWER,
    SCOPE_AUDIT_READ,
    SCOPE_INGEST,
    SCOPE_READ,
    SCOPE_SEARCH,
    SearchService,
    SourceLookupService,
)
from j1.integration.feedback import FeedbackRecord
from j1.jobs.status import ProcessingStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader


# Phase 8 test stub for the deleted SqliteSearchIndexer.
class DummySearchIndexer:
    kind = "null_indexer"

    def __init__(self, *_, **__):
        pass

    def index(self, *_, **__):
        from j1.processing.results import ProcessingResult
        from j1.processing.status import ResultStatus
        return ProcessingResult(status=ResultStatus.SUCCEEDED)

    def search(self, *_, **__):
        return []

    def delete_by_run_id(self, *_, **__):
        return 0


def _now() -> datetime:
    return datetime(2026, 5, 3, 8, 0, 0, tzinfo=timezone.utc)


# ---- Fixtures --------------------------------------------------------


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return DummySearchIndexer(workspace, artifact_registry, registry)
@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, search_indexer,
    feedback_store, audit_recorder,
) -> ApplicationFacade:
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(search_indexer),
    )


@pytest.fixture
def bulk_export_service(registry, artifact_registry, feedback_store):
    return BulkExportService(registry, artifact_registry, feedback_store)


@pytest.fixture
def bulk_import_service(registry):
    return BulkImportService(registry)


@pytest.fixture
def open_client(application_facade, bulk_export_service, bulk_import_service):
    """Anonymous-mode client (no authenticator) — for non-security tests."""
    return TestClient(create_rest_api(
        application_facade,
        bulk_export=bulk_export_service,
        bulk_import=bulk_import_service,
    ))


@pytest.fixture
def authenticator():
    return ApiKeyAuthenticator({
        "tok-reader": ApiKeyRecord(
            subject="svc-reader", tenant_id="acme",
            scopes=frozenset({SCOPE_READ, SCOPE_SEARCH, SCOPE_ANSWER}),
        ),
        "tok-auditor": ApiKeyRecord(
            subject="svc-auditor", tenant_id="acme",
            scopes=frozenset({SCOPE_READ, SCOPE_AUDIT_READ}),
        ),
        "tok-importer": ApiKeyRecord(
            subject="svc-importer", tenant_id="acme",
            scopes=frozenset({SCOPE_INGEST}),
        ),
    })


@pytest.fixture
def secured_client(application_facade, authenticator,
                   bulk_export_service, bulk_import_service):
    return TestClient(create_rest_api(
        application_facade,
        authenticator=authenticator,
        bulk_export=bulk_export_service,
        bulk_import=bulk_import_service,
    ))


def _seed_document(ctx, registry, *, document_id="doc-1",
                   checksum="sha256:doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10, checksum=checksum,
        status=ProcessingStatus.PENDING, created_at=_now(),
    )
    registry.add(record)
    return record


def _headers(*, token: str | None = None,
             tenant: str = "acme", project: str = "alpha") -> dict[str, str]:
    h = {TENANT_HEADER: tenant, PROJECT_HEADER: project}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_ndjson_body(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---- Export endpoints — content type, shape, scope -----------------


def test_export_documents_returns_ndjson(open_client, ctx, registry):
    _seed_document(ctx, registry)
    response = open_client.get("/exports/documents.ndjson", headers=_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "documents.ndjson" in response.headers.get("content-disposition", "")
    records = _parse_ndjson_body(response.text)
    assert len(records) == 1
    assert records[0]["documentId"] == "doc-1"


@pytest.mark.parametrize("path,scope_label", [
    ("/exports/documents.ndjson", "documents"),
    ("/exports/sources.ndjson", "sources"),
    ("/exports/chunks.ndjson", "chunks"),
    ("/exports/citations.ndjson", "citations"),
    ("/exports/metadata.ndjson", "metadata"),
])
def test_read_scope_unlocks_all_read_exports(
    secured_client, ctx, registry, path, scope_label,
):
    _seed_document(ctx, registry)
    response = secured_client.get(path, headers=_headers(token="tok-reader"))
    assert response.status_code == 200, path
    assert response.headers["content-type"].startswith("application/x-ndjson")


def test_feedback_export_requires_audit_read_scope(secured_client):
    # tok-reader has kb:read but NOT kb:audit.read
    response = secured_client.get(
        "/exports/feedback.ndjson", headers=_headers(token="tok-reader"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == SCOPE_AUDIT_READ


def test_feedback_export_succeeds_with_audit_read(
    secured_client, ctx, feedback_store,
):
    feedback_store.add(FeedbackRecord(
        feedback_id="fb-1", project=ctx,
        target_kind="artifact", target_id="a-1",
        submitted_at=_now(), rating=1, comment="ok",
    ))
    response = secured_client.get(
        "/exports/feedback.ndjson", headers=_headers(token="tok-auditor"),
    )
    assert response.status_code == 200
    rows = _parse_ndjson_body(response.text)
    assert len(rows) == 1
    assert rows[0]["feedbackId"] == "fb-1"


def test_export_without_auth_returns_401(secured_client):
    response = secured_client.get("/exports/documents.ndjson", headers=_headers())
    assert response.status_code == 401


def test_export_503_when_capability_not_wired(application_facade):
    # No bulk_export passed
    app = create_rest_api(application_facade)
    client = TestClient(app)
    response = client.get("/exports/documents.ndjson", headers=_headers())
    assert response.status_code == 503


# ---- Import endpoints — happy path + scopes ------------------------


_DOC_PAYLOAD = {
    "documentId": "imported-1",
    "tenantId": "acme",
    "projectId": "alpha",
    "originalFilename": "i.pdf",
    "storedFilename": "imported-1.pdf",
    "mimeType": "application/pdf",
    "fileSize": 4096,
    "checksum": "sha256:imported-1",
    "status": "pending",
    "createdAt": "2026-05-03T08:00:00+00:00",
}


def test_import_documents_returns_envelope_with_counts(open_client, registry, ctx):
    body = (json.dumps(_DOC_PAYLOAD) + "\n").encode("utf-8")
    response = open_client.post(
        "/imports/documents.ndjson", content=body, headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 1
    assert data["skippedIdempotent"] == 0
    assert data["failures"] == []
    assert data["total"] == 1
    # State actually changed
    assert any(d.document_id == "imported-1" for d in registry.list_documents(ctx))


def test_import_documents_requires_kb_ingest_scope(secured_client):
    body = (json.dumps(_DOC_PAYLOAD) + "\n").encode("utf-8")
    response = secured_client.post(
        "/imports/documents.ndjson", content=body,
        headers=_headers(token="tok-reader"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == SCOPE_INGEST


def test_import_documents_succeeds_with_kb_ingest(secured_client, ctx, registry):
    body = (json.dumps(_DOC_PAYLOAD) + "\n").encode("utf-8")
    response = secured_client.post(
        "/imports/documents.ndjson", content=body,
        headers=_headers(token="tok-importer"),
    )
    assert response.status_code == 200
    assert response.json()["data"]["succeeded"] == 1


def test_import_partial_failure_includes_line_number_and_code(open_client):
    bad = json.dumps({"foo": "bar"}).encode("utf-8")  # missing required fields
    body = (
        json.dumps(_DOC_PAYLOAD).encode("utf-8") + b"\n"
        + bad + b"\n"
        + b"{not json\n"
    )
    response = open_client.post(
        "/imports/documents.ndjson", content=body, headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 1
    assert len(data["failures"]) == 2
    line_numbers = {f["lineNumber"] for f in data["failures"]}
    assert line_numbers == {2, 3}
    codes = {f["code"] for f in data["failures"]}
    assert codes == {"SCHEMA_VALIDATION_FAILED", "INVALID_JSON"}


def test_import_rejects_cross_tenant_row(open_client):
    payload = {**_DOC_PAYLOAD, "tenantId": "other-tenant"}
    body = (json.dumps(payload) + "\n").encode("utf-8")
    response = open_client.post(
        "/imports/documents.ndjson", content=body, headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 0
    assert data["failures"][0]["code"] == "PROJECT_MISMATCH"
    assert data["failures"][0]["recordId"] == "imported-1"


# ---- Round-trip: export then re-import is idempotent --------------


def test_round_trip_export_then_reimport_is_idempotent(
    open_client, ctx, registry,
):
    _seed_document(ctx, registry, document_id="rt-1", checksum="sha256:rt-1")
    _seed_document(ctx, registry, document_id="rt-2", checksum="sha256:rt-2")
    exported = open_client.get(
        "/exports/documents.ndjson", headers=_headers(),
    ).text
    response = open_client.post(
        "/imports/documents.ndjson",
        content=exported.encode("utf-8"),
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 0
    assert data["skippedIdempotent"] == 2


# ---- Sources endpoint: alias of documents -------------------------


def test_sources_export_matches_documents_export(open_client, ctx, registry):
    _seed_document(ctx, registry)
    docs_text = open_client.get(
        "/exports/documents.ndjson", headers=_headers(),
    ).text
    sources_text = open_client.get(
        "/exports/sources.ndjson", headers=_headers(),
    ).text
    assert _parse_ndjson_body(docs_text) == _parse_ndjson_body(sources_text)


def test_sources_import_uses_same_shape(open_client, ctx, registry):
    body = (json.dumps(_DOC_PAYLOAD) + "\n").encode("utf-8")
    response = open_client.post(
        "/imports/sources.ndjson", content=body, headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["data"]["succeeded"] == 1


# ---- Metadata import: integrity verification only ------------------


def test_metadata_import_succeeds_for_matching_records(
    open_client, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1", checksum="sha256:doc-1")
    metadata_body = open_client.get(
        "/exports/metadata.ndjson", headers=_headers(),
    ).text
    response = open_client.post(
        "/imports/metadata.ndjson",
        content=metadata_body.encode("utf-8"), headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 1
    assert data["failures"] == []


def test_metadata_import_reports_integrity_mismatch(
    open_client, ctx, registry,
):
    _seed_document(ctx, registry, document_id="doc-1", checksum="sha256:doc-1")
    bad = json.dumps({
        "documentId": "doc-1",
        "tenantId": "acme",
        "projectId": "alpha",
        "originalFilename": "wrong-name.pdf",
        "mimeType": "application/pdf",
        "fileSize": 999999,  # mismatch
        "checksum": "sha256:doc-1",
        "status": "pending",
        "createdAt": "2026-05-03T08:00:00+00:00",
    }).encode("utf-8") + b"\n"
    response = open_client.post(
        "/imports/metadata.ndjson", content=bad, headers=_headers(),
    )
    data = response.json()["data"]
    assert data["succeeded"] == 0
    assert data["failures"][0]["code"] == "INTEGRITY_MISMATCH"


def test_metadata_import_reports_unknown_document(open_client):
    bad = json.dumps({
        "documentId": "missing",
        "tenantId": "acme",
        "projectId": "alpha",
        "originalFilename": "x.pdf",
        "mimeType": "application/pdf",
        "fileSize": 0,
        "checksum": "sha256:x",
        "status": "pending",
        "createdAt": "2026-05-03T08:00:00+00:00",
    }).encode("utf-8") + b"\n"
    response = open_client.post(
        "/imports/metadata.ndjson", content=bad, headers=_headers(),
    )
    data = response.json()["data"]
    assert data["failures"][0]["code"] == "DOCUMENT_NOT_FOUND"


# ---- Capability advertisement -------------------------------------


def test_capabilities_lists_bulk_when_wired(open_client):
    caps = {c["name"]: c for c in open_client.get("/capabilities").json()["data"]["capabilities"]}
    assert caps["bulk.export"]["available"] is True
    assert caps["bulk.import"]["available"] is True


def test_capabilities_marks_bulk_unavailable_when_not_wired(application_facade):
    app = create_rest_api(application_facade)
    client = TestClient(app)
    caps = {c["name"]: c for c in client.get("/capabilities").json()["data"]["capabilities"]}
    assert caps["bulk.export"]["available"] is False
    assert caps["bulk.import"]["available"] is False


# ---- OpenAPI contract --------------------------------------------


def test_openapi_advertises_bulk_endpoints(open_client):
    spec = open_client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    expected = {
        "/exports/documents.ndjson",
        "/exports/sources.ndjson",
        "/exports/chunks.ndjson",
        "/exports/citations.ndjson",
        "/exports/metadata.ndjson",
        "/exports/feedback.ndjson",
        "/imports/documents.ndjson",
        "/imports/sources.ndjson",
        "/imports/metadata.ndjson",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"
