"""End-to-end tests for the REST adapter's authentication / authorization layer.

Verifies:
- public endpoints (/health, /version) bypass auth even when an authenticator is configured
- requests without credentials get a standardized 401 envelope
- invalid bearer / X-API-Key tokens are rejected with 401
- valid token + insufficient scope → 403 INSUFFICIENT_SCOPE with required_scope detail
- valid token + correct scope → 200
- both Authorization: Bearer and X-API-Key are accepted
- security context is propagated to handlers (verified via /feedback's actor)
- the request_id header is round-tripped on auth failures too
- auth disabled (authenticator=None) → existing anonymous behaviour
"""

import io
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    REQUEST_ID_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.documents.models import DocumentRecord
from j1.integration import (
    AnswerService,
    ApiKeyAuthenticator,
    ApiKeyRecord,
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SCOPE_ADMIN,
    SCOPE_ANSWER,
    SCOPE_FEEDBACK,
    SCOPE_INGEST,
    SCOPE_READ,
    SCOPE_RETRIEVE,
    SCOPE_SEARCH,
    SearchService,
    SourceLookupService,
)
from j1.jobs.status import ProcessingStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.search.indexer import SqliteSearchIndexer


# ---- Fixtures --------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, search_indexer):
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(search_indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(search_indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(search_indexer, profile),
    )


@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, search_indexer,
    query_engine, feedback_store, audit_recorder,
) -> ApplicationFacade:
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(search_indexer),
        answer=AnswerService(query_engine),
    )


@pytest.fixture
def authenticator() -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator({
        "tok-readonly": ApiKeyRecord(
            subject="svc-readonly", tenant_id="acme",
            scopes=frozenset({SCOPE_READ, SCOPE_SEARCH}),
        ),
        "tok-power": ApiKeyRecord(
            subject="svc-power", tenant_id="acme",
            scopes=frozenset({
                SCOPE_READ, SCOPE_SEARCH, SCOPE_RETRIEVE, SCOPE_ANSWER,
                SCOPE_INGEST, SCOPE_FEEDBACK, SCOPE_ADMIN,
            }),
        ),
        "tok-noscope": ApiKeyRecord(
            subject="svc-noscope", tenant_id="acme",
            scopes=frozenset(),
        ),
    })


@pytest.fixture
def secure_client(application_facade, authenticator) -> TestClient:
    app = create_rest_api(
        application_facade,
        authenticator=authenticator,
        version="2.0.0",
    )
    return TestClient(app)


def _stage_document(ctx, registry, document_id: str = "doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


def _headers(
    *, token: str | None = None, api_key: str | None = None,
    tenant: str = "acme", project: str = "alpha",
) -> dict[str, str]:
    h = {TENANT_HEADER: tenant, PROJECT_HEADER: project}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if api_key is not None:
        h["X-API-Key"] = api_key
    return h


def _assert_error_envelope(payload: dict) -> dict:
    assert "requestId" in payload
    assert "error" in payload
    err = payload["error"]
    for k in ("code", "message", "details"):
        assert k in err
    return err


# ---- Public endpoints bypass auth -----------------------------------


def test_health_is_public_even_with_auth_enabled(secure_client):
    response = secure_client.get("/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


def test_version_is_public_even_with_auth_enabled(secure_client):
    response = secure_client.get("/version")
    assert response.status_code == 200
    assert response.json()["data"]["version"] == "2.0.0"


# ---- Missing / invalid credentials ----------------------------------


def test_protected_endpoint_without_credential_returns_401(secure_client):
    response = secure_client.get("/documents/anything", headers=_headers())
    assert response.status_code == 401
    err = _assert_error_envelope(response.json())
    assert err["code"] == "UNAUTHENTICATED"
    assert "missing" in err["message"].lower()


def test_invalid_bearer_token_returns_401(secure_client):
    response = secure_client.get(
        "/documents/anything", headers=_headers(token="nope"),
    )
    assert response.status_code == 401
    err = _assert_error_envelope(response.json())
    assert err["code"] == "UNAUTHENTICATED"


def test_invalid_api_key_header_returns_401(secure_client):
    response = secure_client.get(
        "/documents/anything", headers=_headers(api_key="nope"),
    )
    assert response.status_code == 401


def test_unsupported_authorization_scheme_treated_as_missing(secure_client):
    """`Authorization: Basic ...` is not a credential we accept — should 401."""
    headers = _headers()
    headers["Authorization"] = "Basic abc=="
    response = secure_client.get("/documents/anything", headers=headers)
    assert response.status_code == 401


def test_auth_failure_envelopes_include_request_id(secure_client):
    response = secure_client.get("/documents/x", headers=_headers())
    assert response.status_code == 401
    body = response.json()
    assert REQUEST_ID_HEADER in response.headers
    assert body["requestId"] == response.headers[REQUEST_ID_HEADER]


# ---- Valid auth, scope checks ---------------------------------------


def test_valid_bearer_with_correct_scope_succeeds(
    secure_client, ctx, registry,
):
    _stage_document(ctx, registry, document_id="doc-x")
    response = secure_client.get(
        "/documents/doc-x", headers=_headers(token="tok-readonly"),
    )
    assert response.status_code == 200
    assert response.json()["data"]["documentId"] == "doc-x"


def test_valid_api_key_with_correct_scope_succeeds(
    secure_client, ctx, registry,
):
    _stage_document(ctx, registry, document_id="doc-x")
    response = secure_client.get(
        "/documents/doc-x", headers=_headers(api_key="tok-readonly"),
    )
    assert response.status_code == 200


def test_valid_token_missing_scope_returns_403(secure_client, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-x")
    response = secure_client.post(
        "/documents/doc-x/ingest",
        json={"compilerKind": "x"},
        headers=_headers(token="tok-readonly"),  # readonly lacks kb:ingest
    )
    assert response.status_code == 403
    err = _assert_error_envelope(response.json())
    assert err["code"] == "INSUFFICIENT_SCOPE"
    assert err["details"]["required_scope"] == "kb:ingest"


def test_token_with_no_scopes_blocked_from_everything_but_public(
    secure_client, ctx, registry,
):
    _stage_document(ctx, registry, document_id="doc-x")
    response = secure_client.get(
        "/documents/doc-x", headers=_headers(token="tok-noscope"),
    )
    assert response.status_code == 403


def test_search_endpoint_requires_kb_search(secure_client):
    response = secure_client.post(
        "/search", json={"query": "x"},
        headers=_headers(token="tok-noscope"),
    )
    assert response.status_code == 403
    err = _assert_error_envelope(response.json())
    assert err["details"]["required_scope"] == SCOPE_SEARCH


def test_answer_endpoint_requires_kb_answer(secure_client):
    response = secure_client.post(
        "/answer", json={"question": "x"},
        headers=_headers(token="tok-readonly"),  # no kb:answer
    )
    assert response.status_code == 403
    err = _assert_error_envelope(response.json())
    assert err["details"]["required_scope"] == SCOPE_ANSWER


def test_feedback_requires_kb_feedback(secure_client):
    response = secure_client.post(
        "/feedback",
        json={"targetKind": "artifact", "targetId": "a"},
        headers=_headers(token="tok-readonly"),
    )
    assert response.status_code == 403


def test_admin_scope_unlocks_pause_endpoint(secure_client):
    response = secure_client.post(
        "/ingestion-jobs/wf-1/pause",
        headers=_headers(token="tok-power"),
    )
    # job_control wasn't wired into this fixture's facade → 503, not 403.
    # Either way, the auth gate passed (no 401 / 403).
    assert response.status_code in (200, 503)


# ---- Capabilities is protected --------------------------------------


def test_capabilities_requires_kb_read(secure_client):
    response = secure_client.get(
        "/capabilities", headers=_headers(token="tok-noscope"),
    )
    assert response.status_code == 403


def test_capabilities_succeeds_with_kb_read(secure_client):
    response = secure_client.get(
        "/capabilities", headers=_headers(token="tok-readonly"),
    )
    assert response.status_code == 200


# ---- Security context propagation -----------------------------------


def test_feedback_uses_authenticated_subject_as_actor(
    secure_client, workspace, ctx, feedback_store,
):
    response = secure_client.post(
        "/feedback",
        json={"targetKind": "artifact", "targetId": "a-1", "rating": 1},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    # Caller didn't pass `actor`; the authenticated subject is used instead.
    records = list(feedback_store.list_for(ctx))
    assert len(records) == 1
    assert records[0].actor == "svc-power"


def test_feedback_explicit_actor_overrides_subject(
    secure_client, ctx, feedback_store,
):
    response = secure_client.post(
        "/feedback",
        json={
            "targetKind": "artifact", "targetId": "a-1", "rating": 1,
            "actor": "human@example.com",
        },
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    records = list(feedback_store.list_for(ctx))
    assert records[0].actor == "human@example.com"


# ---- Auth disabled (authenticator=None) --- back-compat path -------


def test_no_authenticator_means_anonymous_full_access(application_facade, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-x")
    app = create_rest_api(application_facade)  # authenticator omitted
    c = TestClient(app)
    response = c.get("/documents/doc-x", headers=_headers())
    assert response.status_code == 200


def test_anonymous_paths_can_be_overridden(application_facade, authenticator):
    """Custom anonymous_paths replaces the default set."""
    app = create_rest_api(
        application_facade,
        authenticator=authenticator,
        anonymous_paths=frozenset({"/health"}),  # /version no longer public
    )
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/version").status_code == 401


# ---- Security context shape -----------------------------------------


def test_security_context_carries_request_id(
    secure_client, ctx, registry,
):
    """The middleware should attach the request_id to the security context."""
    _stage_document(ctx, registry, document_id="doc-x")
    response = secure_client.get(
        "/documents/doc-x", headers=_headers(token="tok-readonly"),
    )
    assert response.status_code == 200
    # Round-trip via X-Request-Id header is the observable proof.
    assert REQUEST_ID_HEADER in response.headers
    assert response.json()["requestId"] == response.headers[REQUEST_ID_HEADER]


# ---- OpenAPI / Swagger UI: Authorize button --------------------------


def test_openapi_advertises_bearer_and_api_key_when_auth_enabled(secure_client):
    """When `authenticator=` is set, the OpenAPI document must
    declare the Bearer + API-key security schemes so Swagger UI
    renders the Authorize button. Without these schemes the
    operator can't test protected endpoints from the docs page."""
    spec = secure_client.get("/openapi.json").json()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "bearer" in schemes, f"bearer scheme missing from {schemes}"
    assert "api_key" in schemes, f"api_key scheme missing from {schemes}"
    bearer = schemes["bearer"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer.get("description"), "bearer scheme needs description"
    api_key = schemes["api_key"]
    assert api_key["type"] == "apiKey"
    assert api_key["in"] == "header"
    assert api_key["name"] == "X-API-Key"


def test_openapi_marks_anonymous_paths_as_unauthenticated(secure_client):
    """`/health` and `/version` are exempt from auth even when
    `authenticator=` is set. Their OpenAPI operations must carry an
    empty `security: []` so Swagger UI doesn't pretend they need a
    credential."""
    spec = secure_client.get("/openapi.json").json()
    health = spec["paths"]["/health"]["get"]
    version = spec["paths"]["/version"]["get"]
    assert health.get("security") == [], (
        f"/health must override security to []; got {health.get('security')}"
    )
    assert version.get("security") == [], (
        f"/version must override security to []; got {version.get('security')}"
    )


def test_openapi_protected_paths_inherit_global_security(secure_client):
    """Protected operations don't carry their own `security` (they
    inherit from the document-level `security` array). Swagger
    renders the lock icon based on the global default."""
    spec = secure_client.get("/openapi.json").json()
    # Global security advertises both schemes — operator can pick either.
    global_security = spec.get("security")
    assert global_security == [{"bearer": []}, {"api_key": []}], (
        f"unexpected global security: {global_security}"
    )
    # A representative protected endpoint must NOT override security
    # (no per-op security key → inherits global).
    documents_post = spec["paths"]["/documents"]["post"]
    assert "security" not in documents_post or documents_post["security"] != [], (
        f"/documents POST must inherit global security; "
        f"got per-op override: {documents_post.get('security')}"
    )
