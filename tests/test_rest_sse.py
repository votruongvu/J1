"""Tests for `POST /answer?stream=true` Server-Sent Events streaming.

Covers:
- Existing non-streaming /answer behaviour is preserved (back-compat).
- Streaming endpoint returns Content-Type: text/event-stream.
- The same auth + scope rules apply as non-streaming /answer.
- Unauthenticated streaming → 401; missing kb:answer scope → 403.
- Expected SSE event order: started → retrieval.* → generation.delta+
 → citation.added* → completed.
- generation.delta events emitted for non-empty answers.
- citation.added events emitted when sources are present.
- requestId is included in every SSE payload.
- answer.failed is emitted (with safe masked payload) when the
 underlying answer service raises after the stream is open.
- answer.failed payload does NOT leak raw exception text.
- Streaming uses the same request-body validation as non-streaming.
- Tests for the application-level streaming primitives (chunking,
 citation events, error masking) live alongside the REST tests so the
 integration boundary is exercised too.
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    REQUEST_ID_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.adapters.rest.sse import SSE_CONTENT_TYPE, format_sse
from j1.artifacts.models import ArtifactRecord
from j1.integration import (
    AnswerService,
    AnswerStreamEvent,
    AnswerStreamingService,
    ApiKeyAuthenticator,
    ApiKeyRecord,
    ApplicationFacade,
    BufferingStreamHandler,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SAFE_GENERATION_FAILED_PAYLOAD,
    SCOPE_ANSWER,
    SCOPE_READ,
    SCOPE_SEARCH,
    STREAM_EVENT_ANSWER_COMPLETED,
    STREAM_EVENT_ANSWER_FAILED,
    STREAM_EVENT_ANSWER_STARTED,
    STREAM_EVENT_CITATION_ADDED,
    STREAM_EVENT_GENERATION_DELTA,
    STREAM_EVENT_RETRIEVAL_COMPLETED,
    STREAM_EVENT_RETRIEVAL_STARTED,
    SearchService,
    SourceLookupService,
)
from j1.integration.dto import AnswerDTO, AnswerRequestDTO, CitationDTO
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader
from j1.projects.context import ProjectContext
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
from j1.workspace.layout import WorkspaceArea


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
        "tok-answer": ApiKeyRecord(
            subject="svc-answer", tenant_id="acme",
            scopes=frozenset({SCOPE_READ, SCOPE_SEARCH, SCOPE_ANSWER}),
        ),
        "tok-readonly": ApiKeyRecord(
            subject="svc-readonly", tenant_id="acme",
            scopes=frozenset({SCOPE_READ}),
        ),
    })


@pytest.fixture
def open_client(application_facade) -> TestClient:
    """No authenticator wired — anonymous access (existing default)."""
    return TestClient(create_rest_api(application_facade))


@pytest.fixture
def secured_client(application_facade, authenticator) -> TestClient:
    return TestClient(create_rest_api(
        application_facade, authenticator=authenticator,
    ))


def _headers(*, token: str | None = None,
             tenant: str = "acme", project: str = "alpha") -> dict[str, str]:
    h = {TENANT_HEADER: tenant, PROJECT_HEADER: project}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


def _stage_artifact(workspace, ctx, artifact_registry, *, artifact_id, content):
    area_dir = workspace.area(ctx, WorkspaceArea.COMPILED)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / f"{artifact_id}.txt").write_bytes(content)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id, project=ctx, kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}", byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_now(), updated_at=_now(),
        source_document_ids=["doc-1"],
    ))


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE response body into (event_name, data_dict) tuples."""
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = None
        data_str = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_str = line[len("data: "):]
        if event_name and data_str is not None:
            events.append((event_name, json.loads(data_str)))
    return events


# ---- Back-compat: non-streaming /answer is unchanged ---------------


def test_non_streaming_answer_still_returns_envelope(open_client):
    response = open_client.post(
        "/answer", json={"question": "anything"}, headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert "requestId" in body and "data" in body
    # No SSE — normal JSON content type
    assert response.headers["content-type"].startswith("application/json")


def test_stream_query_param_omitted_means_normal_json(open_client):
    """`?stream=false` and missing query both → JSON envelope."""
    r1 = open_client.post(
        "/answer?stream=false", json={"question": "x"}, headers=_headers(),
    )
    r2 = open_client.post(
        "/answer", json={"question": "x"}, headers=_headers(),
    )
    assert r1.headers["content-type"].startswith("application/json")
    assert r2.headers["content-type"].startswith("application/json")


# ---- Content type + headers ----------------------------------------


def test_streaming_response_uses_text_event_stream(open_client):
    response = open_client.post(
        "/answer?stream=true", json={"question": "anything"},
        headers=_headers(),
    )
    assert response.status_code == 200
    # `text/event-stream; charset=...` — startswith handles the suffix
    assert response.headers["content-type"].startswith(SSE_CONTENT_TYPE)
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"


# ---- Same security rules as non-streaming --------------------------


def test_streaming_requires_auth_when_authenticator_wired(secured_client):
    response = secured_client.post(
        "/answer?stream=true", json={"question": "x"}, headers=_headers(),
    )
    assert response.status_code == 401
    err = response.json()["error"]
    assert err["code"] == "UNAUTHENTICATED"


def test_streaming_requires_kb_answer_scope(secured_client):
    response = secured_client.post(
        "/answer?stream=true", json={"question": "x"},
        headers=_headers(token="tok-readonly"),
    )
    assert response.status_code == 403
    err = response.json()["error"]
    assert err["code"] == "INSUFFICIENT_SCOPE"
    assert err["details"]["required_scope"] == SCOPE_ANSWER


def test_streaming_with_valid_scope_succeeds(secured_client):
    response = secured_client.post(
        "/answer?stream=true", json={"question": "x"},
        headers=_headers(token="tok-answer"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SSE_CONTENT_TYPE)


def test_streaming_validates_request_body(open_client):
    """Empty question → 422 (Pydantic), same as non-streaming."""
    response = open_client.post(
        "/answer?stream=true", json={"question": ""}, headers=_headers(),
    )
    assert response.status_code == 422


# ---- Event ordering + payload shape --------------------------------


def test_streaming_emits_lifecycle_events_in_order(
    open_client, ctx, artifact_registry, search_indexer, workspace,
):
    _stage_artifact(workspace, ctx, artifact_registry,
                    artifact_id="a-1",
                    content=b"the schedule constraint is firm")
    search_indexer.index(ctx, ["a-1"])
    response = open_client.post(
        "/answer?stream=true", json={"question": "schedule"},
        headers=_headers(),
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    types = [e[0] for e in events]
    assert types[0] == STREAM_EVENT_ANSWER_STARTED
    assert types[1] == STREAM_EVENT_RETRIEVAL_STARTED
    assert types[-1] == STREAM_EVENT_ANSWER_COMPLETED
    # retrieval.completed must come before any generation.delta
    assert types.index(STREAM_EVENT_RETRIEVAL_COMPLETED) < (
        types.index(STREAM_EVENT_GENERATION_DELTA)
        if STREAM_EVENT_GENERATION_DELTA in types
        else len(types)
    )


def test_streaming_emits_generation_delta_for_nonempty_answer(
    open_client, ctx, artifact_registry, search_indexer, workspace,
):
    _stage_artifact(workspace, ctx, artifact_registry,
                    artifact_id="a-1",
                    content=b"the schedule constraint is firm")
    search_indexer.index(ctx, ["a-1"])
    response = open_client.post(
        "/answer?stream=true", json={"question": "schedule"},
        headers=_headers(),
    )
    events = _parse_sse(response.text)
    deltas = [e for e in events if e[0] == STREAM_EVENT_GENERATION_DELTA]
    assert deltas, "expected at least one generation.delta event"
    for _, payload in deltas:
        assert "text" in payload["data"]
        assert isinstance(payload["data"]["text"], str)


def test_streaming_emits_citation_added_per_source(
    open_client, ctx, artifact_registry, search_indexer, workspace,
):
    _stage_artifact(workspace, ctx, artifact_registry,
                    artifact_id="a-1",
                    content=b"the schedule constraint is firm")
    search_indexer.index(ctx, ["a-1"])
    response = open_client.post(
        "/answer?stream=true", json={"question": "schedule"},
        headers=_headers(),
    )
    events = _parse_sse(response.text)
    citations = [e for e in events if e[0] == STREAM_EVENT_CITATION_ADDED]
    assert citations, "expected at least one citation.added event"
    for _, payload in citations:
        assert "artifactId" in payload["data"]


def test_every_sse_payload_includes_request_id(open_client):
    response = open_client.post(
        "/answer?stream=true", json={"question": "x"}, headers=_headers(),
    )
    events = _parse_sse(response.text)
    request_id = response.headers[REQUEST_ID_HEADER]
    assert events  # at least answer.started + completed
    for _, payload in events:
        assert payload["requestId"] == request_id


# ---- Failure path: answer.failed with masked payload ---------------


class _ExplodingAnswerPort:
    """Drop-in replacement that raises during answer."""

    def answer(self, ctx, request):
        raise RuntimeError("provider returned 503: secret_internal_path=/etc/...")


@pytest.fixture
def exploding_facade(application_facade) -> ApplicationFacade:
    return ApplicationFacade(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=application_facade.source_lookup,
        feedback=application_facade.feedback,
        event_publisher=application_facade.event_publisher,
        search=application_facade.search,
        answer=_ExplodingAnswerPort(),
    )


def test_streaming_emits_answer_failed_on_engine_exception(exploding_facade):
    client = TestClient(create_rest_api(exploding_facade))
    response = client.post(
        "/answer?stream=true", json={"question": "x"}, headers=_headers(),
    )
    assert response.status_code == 200  # stream opened, then failed
    events = _parse_sse(response.text)
    types = [e[0] for e in events]
    assert STREAM_EVENT_ANSWER_FAILED in types
    # Must NOT progress past the failure
    assert STREAM_EVENT_ANSWER_COMPLETED not in types
    assert STREAM_EVENT_GENERATION_DELTA not in types


def test_failed_payload_does_not_leak_exception_details(exploding_facade):
    client = TestClient(create_rest_api(exploding_facade))
    response = client.post(
        "/answer?stream=true", json={"question": "x"}, headers=_headers(),
    )
    events = _parse_sse(response.text)
    failures = [e for e in events if e[0] == STREAM_EVENT_ANSWER_FAILED]
    assert len(failures) == 1
    payload = failures[0][1]["data"]
    # Matches the safe-payload contract exactly.
    assert payload == SAFE_GENERATION_FAILED_PAYLOAD
    # No raw exception text leaked into the wire body.
    assert "secret_internal_path" not in response.text
    assert "RuntimeError" not in response.text
    assert "/etc/" not in response.text


# ---- Application-level streaming service primitives ----------------


class _StubAnswerPort:
    def __init__(self, dto: AnswerDTO) -> None:
        self._dto = dto

    def answer(self, ctx, request):
        return self._dto


def _stub_dto(text: str, citations: list[CitationDTO] | None = None) -> AnswerDTO:
    return AnswerDTO(
        answer=text,
        mode_used="auto",
        sources=citations or [],
        related_artifacts=[],
        graph_paths=[],
        confidence=0.7,
        confidence_level="medium",
        review_required=False,
        warnings=[],
        warning_categories=[],
    )


def test_streaming_service_chunks_text_into_word_batches():
    text = " ".join(["word"] * 25)
    svc = AnswerStreamingService(_StubAnswerPort(_stub_dto(text)),
                                 words_per_delta=8)
    handler = BufferingStreamHandler()
    svc.stream(
        ProjectContext("acme", "alpha"),
        AnswerRequestDTO(question="x"),
        request_id="req-1", handler=handler,
    )
    deltas = handler.by_event(STREAM_EVENT_GENERATION_DELTA)
    # 25 words / 8 per chunk = 4 chunks (8, 8, 8, 1)
    assert len(deltas) == 4
    chunks = [d.data["text"] for d in deltas]
    assert sum(len(c.split()) for c in chunks) == 25


def test_streaming_service_emits_no_delta_for_empty_answer():
    svc = AnswerStreamingService(_StubAnswerPort(_stub_dto("")))
    handler = BufferingStreamHandler()
    svc.stream(
        ProjectContext("acme", "alpha"),
        AnswerRequestDTO(question="x"),
        request_id="req-1", handler=handler,
    )
    assert handler.by_event(STREAM_EVENT_GENERATION_DELTA) == []
    # Lifecycle still completes
    assert handler.by_event(STREAM_EVENT_ANSWER_COMPLETED)


def test_streaming_service_emits_citation_added_per_source():
    citations = [
        CitationDTO(artifact_id="a-1", artifact_type="t",
                    source_document_id="doc-1"),
        CitationDTO(artifact_id="a-2", artifact_type="t",
                    source_document_id="doc-2"),
    ]
    svc = AnswerStreamingService(_StubAnswerPort(_stub_dto("hi", citations)))
    handler = BufferingStreamHandler()
    svc.stream(
        ProjectContext("acme", "alpha"),
        AnswerRequestDTO(question="x"),
        request_id="req-1", handler=handler,
    )
    cits = handler.by_event(STREAM_EVENT_CITATION_ADDED)
    assert [c.data["artifactId"] for c in cits] == ["a-1", "a-2"]


def test_streaming_service_masks_failure_into_answer_failed():
    svc = AnswerStreamingService(_ExplodingAnswerPort())
    handler = BufferingStreamHandler()
    result = svc.stream(
        ProjectContext("acme", "alpha"),
        AnswerRequestDTO(question="x"),
        request_id="req-1", handler=handler,
    )
    assert result is None
    types = [e.event for e in handler.events]
    assert STREAM_EVENT_ANSWER_FAILED in types
    # No completion + no delta when failure happens during retrieval
    assert STREAM_EVENT_ANSWER_COMPLETED not in types
    failure = handler.by_event(STREAM_EVENT_ANSWER_FAILED)[0]
    assert failure.data == SAFE_GENERATION_FAILED_PAYLOAD


def test_streaming_service_emits_request_id_on_every_event():
    svc = AnswerStreamingService(_StubAnswerPort(_stub_dto("hi there")))
    handler = BufferingStreamHandler()
    svc.stream(
        ProjectContext("acme", "alpha"),
        AnswerRequestDTO(question="x"),
        request_id="req-fixed", handler=handler,
    )
    assert all(e.request_id == "req-fixed" for e in handler.events)


# ---- SSE wire-format helper ----------------------------------------


def test_format_sse_emits_event_and_data_lines():
    event = AnswerStreamEvent(
        request_id="req-1", event=STREAM_EVENT_GENERATION_DELTA,
        data={"text": "hello world"},
    )
    raw = format_sse(event).decode("utf-8")
    assert raw.startswith(f"event: {STREAM_EVENT_GENERATION_DELTA}\n")
    assert "data: " in raw
    assert raw.endswith("\n\n")
    # Data line is parseable JSON with the standard envelope shape
    data_line = [l for l in raw.splitlines() if l.startswith("data: ")][0]
    parsed = json.loads(data_line[len("data: "):])
    assert parsed["requestId"] == "req-1"
    assert parsed["event"] == STREAM_EVENT_GENERATION_DELTA
    assert parsed["data"] == {"text": "hello world"}
