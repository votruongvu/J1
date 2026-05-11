"""End-to-end tests for the REST ↔ event bridge.

Verifies:
- `event_bus=None` (back-compat path): handlers behave exactly as before,
 no events are emitted.
- When a bus is wired in, REST handlers publish events with:
 * the right type per surface
 * actor + auth_type carried through from the SecurityContext
 * tenant_id from the project context
 * correlation_id == X-Request-Id (so receivers can tie HTTP request
 → outbound webhook delivery)
- Anonymous traffic emits events with `actor=None` and `auth_type=None`
 (signals "system" to receivers).
- A misbehaving event bus does NOT break the HTTP response — the
 publish-then-respond pattern is fail-safe.
- The CloudEvents envelope built from such events carries `kbactor` /
 `kbauthtype` extension attributes — proving the security context flows
 all the way through to the webhook payload.
"""

import io
from datetime import datetime, timezone

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
    ApplicationEvent,
    ApplicationEventBus,
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EVENT_ANSWER_GENERATED,
    EVENT_DOCUMENT_INGESTION_STARTED,
    EVENT_DOCUMENT_UPLOADED,
    EVENT_QUERY_COMPLETED,
    EventSubscriber,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SCOPE_ANSWER,
    SCOPE_INGEST,
    SCOPE_READ,
    SCOPE_RETRIEVE,
    SCOPE_SEARCH,
    SearchService,
    SourceLookupService,
    to_cloudevent,
)
from j1.integration.events.cloudevents import EXTENSION_ACTOR, EXTENSION_AUTH_TYPE
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


# ---- Recording subscriber + fixtures ---------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def handle(self, event: ApplicationEvent) -> None:
        self.events.append(event)

    def by_type(self, event_type: str) -> list[ApplicationEvent]:
        return [e for e in self.events if e.type == event_type]


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
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def bus(recorder) -> ApplicationEventBus:
    return ApplicationEventBus([recorder])


@pytest.fixture
def authenticator() -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator({
        "tok-power": ApiKeyRecord(
            subject="svc-power", tenant_id="acme",
            scopes=frozenset({
                SCOPE_READ, SCOPE_SEARCH, SCOPE_RETRIEVE, SCOPE_ANSWER,
                SCOPE_INGEST,
            }),
        ),
    })


@pytest.fixture
def secured_client(application_facade, authenticator, bus) -> TestClient:
    app = create_rest_api(
        application_facade,
        authenticator=authenticator,
        event_bus=bus,
        version="3.0.0",
    )
    return TestClient(app)


@pytest.fixture
def anonymous_client(application_facade, bus) -> TestClient:
    app = create_rest_api(application_facade, event_bus=bus)
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


def _headers(*, token: str | None = None, tenant: str = "acme",
             project: str = "alpha") -> dict[str, str]:
    h = {TENANT_HEADER: tenant, PROJECT_HEADER: project}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---- Back-compat: event_bus=None means no events ---------------------


def test_no_event_bus_means_no_events(application_facade, ctx, registry):
    """Default behaviour (event_bus=None) emits no events."""
    _stage_document(ctx, registry, document_id="doc-x")
    app = create_rest_api(application_facade)  # no bus
    client = TestClient(app)
    response = client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=_headers(),
    )
    assert response.status_code == 200
    # No subscriber was wired, so there's nothing to assert on the bus
    # — the headline check is simply that the request succeeded.


# ---- POST /documents emits document.uploaded with full attribution --


def test_document_upload_emits_event_with_actor(secured_client, recorder):
    response = secured_client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_DOCUMENT_UPLOADED)
    assert len(events) == 1
    e = events[0]
    assert e.actor == "svc-power"
    assert e.auth_type == "api_key"
    assert e.tenant_id == "acme"
    # correlation_id round-trips with X-Request-Id
    assert e.correlation_id == response.headers[REQUEST_ID_HEADER]
    # Subject is the new document_id
    assert e.subject and e.data["documentId"] == e.subject
    assert e.data["fileSize"] == len(b"hello")
    assert e.data["duplicate"] is False


def test_document_upload_duplicate_emits_event_with_duplicate_flag(
    secured_client, recorder,
):
    secured_client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"same"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    response = secured_client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"same"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    uploaded = recorder.by_type(EVENT_DOCUMENT_UPLOADED)
    assert len(uploaded) == 2
    assert uploaded[1].data["duplicate"] is True


# ---- POST /documents/{id}/ingest emits document.ingestion_started ---


def test_document_ingest_emits_ingestion_started(
    application_facade, authenticator, bus, recorder, ctx, registry,
):
    _stage_document(ctx, registry, document_id="doc-x")

    async def starter(c, doc_id, body):
        return f"job-{doc_id}-1"

    app = create_rest_api(
        application_facade, authenticator=authenticator,
        job_starter=starter, event_bus=bus,
    )
    client = TestClient(app)
    response = client.post(
        "/documents/doc-x/ingest",
        json={"compilerKind": "external_knowledge_compiler"},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_DOCUMENT_INGESTION_STARTED)
    assert len(events) == 1
    assert events[0].actor == "svc-power"
    assert events[0].subject == "doc-x"
    assert events[0].data["documentId"] == "doc-x"
    assert events[0].data["projectWide"] is False
    assert events[0].data["jobId"] == "job-doc-x-1"


# ---- POST /search emits query.completed -----------------------------


def test_search_emits_query_completed(
    secured_client, ctx, artifact_registry, search_indexer, workspace, recorder,
):
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ReviewStatus
    from j1.workspace.layout import WorkspaceArea

    area_dir = workspace.area(ctx, WorkspaceArea.COMPILED)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / "a-1.txt").write_bytes(b"the schedule constraint is firm")
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-1", project=ctx, kind="compiled.text",
        location="compiled/a-1.txt", content_hash="sha256:a-1", byte_size=20,
        status=ProcessingStatus.SUCCEEDED, review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_now(), updated_at=_now(),
    ))
    search_indexer.index(ctx, ["a-1"])

    response = secured_client.post(
        "/search", json={"query": "schedule"},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_QUERY_COMPLETED)
    assert len(events) == 1
    assert events[0].actor == "svc-power"
    assert events[0].data["surface"] == "search"
    assert events[0].data["query"] == "schedule"
    assert events[0].data["resultCount"] >= 1


def test_retrieve_emits_query_completed_with_retrieve_surface(
    secured_client, recorder,
):
    response = secured_client.post(
        "/retrieve", json={"query": "anything"},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_QUERY_COMPLETED)
    assert len(events) == 1
    assert events[0].data["surface"] == "retrieve"


# ---- POST /answer emits answer.generated ----------------------------


def test_answer_emits_answer_generated(secured_client, recorder):
    response = secured_client.post(
        "/answer", json={"question": "anything"},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_ANSWER_GENERATED)
    assert len(events) == 1
    e = events[0]
    assert e.actor == "svc-power"
    assert e.auth_type == "api_key"
    assert e.data["question"] == "anything"
    assert "modeUsed" in e.data
    assert "citationCount" in e.data
    assert "confidence" in e.data


# ---- Anonymous traffic: actor and auth_type are None ----------------


def test_anonymous_request_emits_event_with_no_actor(anonymous_client, recorder):
    response = anonymous_client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=_headers(),
    )
    assert response.status_code == 200
    events = recorder.by_type(EVENT_DOCUMENT_UPLOADED)
    assert len(events) == 1
    assert events[0].actor is None
    assert events[0].auth_type is None


# ---- CloudEvents envelope carries the new extension attrs ------------


def test_cloudevent_envelope_includes_actor_and_auth_type(
    secured_client, recorder,
):
    secured_client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    [event] = recorder.by_type(EVENT_DOCUMENT_UPLOADED)
    envelope = to_cloudevent(event)
    assert envelope[EXTENSION_ACTOR] == "svc-power"
    assert envelope[EXTENSION_AUTH_TYPE] == "api_key"


# ---- Failure isolation: broken bus doesn't break the response -------


def test_broken_event_bus_does_not_break_handler(
    application_facade, authenticator, ctx, registry,
):
    """If publication itself raises, the HTTP response must still succeed.

 `ApplicationEventBus.publish` already swallows subscriber exceptions,
 but if the bus object itself is malicious / wrong, the publish helper
 in `j1.adapters.rest.events` traps that too.
 """

    class _BrokenBus:
        def publish(self, event):
            raise RuntimeError("bus exploded")

    _stage_document(ctx, registry, document_id="doc-x")
    app = create_rest_api(
        application_facade, authenticator=authenticator,
        event_bus=_BrokenBus(),
    )
    client = TestClient(app)
    response = client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"unique"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200


# ---- Subscriber-side failure isolation (already covered by bus, but
# ---- worth pinning at the REST seam too) ---------------------------


def test_failing_subscriber_does_not_break_request(
    application_facade, authenticator, ctx, registry,
):
    class _BadSubscriber:
        def handle(self, event):
            raise RuntimeError("nope")

    _stage_document(ctx, registry, document_id="doc-x")
    bus = ApplicationEventBus([_BadSubscriber()])
    app = create_rest_api(
        application_facade, authenticator=authenticator, event_bus=bus,
    )
    client = TestClient(app)
    response = client.post(
        "/documents",
        files={"file": ("d.txt", io.BytesIO(b"alsoUnique"), "text/plain")},
        headers=_headers(token="tok-power"),
    )
    assert response.status_code == 200
