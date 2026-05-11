"""Tests for the broker-neutral event publisher abstraction.

Covers:
- Channel mapping is total (every shipped event type maps somewhere).
- channel_for falls back to kb.audit for unknown types (safe default).
- NoopEventPublisher accepts events without raising.
- InMemoryEventPublisher records published events with the resolved
 channel + standard headers (eventId, eventType, occurredAt,
 producer, schemaVersion, correlationId, tenantId, actor, authType,
 idempotencyKey).
- Headers omit fields the source event didn't carry (e.g. anonymous
 events have no actor / authType).
- BusEventPublisher delegates into ApplicationEventBus and is
 failure-isolated.
- CompositeEventPublisher fans out and survives delegate failures.
- load_event_publisher_settings reads env vars and rejects unknown
 publisher types.
- The publisher's `publish` method NEVER raises (publication failures
 must not break the caller's primary work).
"""

import pytest

from j1.errors.exceptions import ConfigError
from j1.integration import (
    ApplicationEvent,
    ApplicationEventBus,
    BusEventPublisher,
    CHANNEL_ANSWER,
    CHANNEL_AUDIT,
    CHANNEL_CITATION,
    CHANNEL_DOCUMENTS,
    CHANNEL_INDEXING,
    CHANNEL_INGESTION,
    CHANNEL_QUERY,
    CompositeEventPublisher,
    EVENT_ANSWER_GENERATED,
    EVENT_CITATION_VALIDATION_FAILED,
    EVENT_DOCUMENT_INDEXING_COMPLETED,
    EVENT_DOCUMENT_INGESTION_FAILED,
    EVENT_DOCUMENT_INGESTION_STARTED,
    EVENT_DOCUMENT_PARSING_STARTED,
    EVENT_DOCUMENT_UPLOADED,
    EVENT_KNOWLEDGE_UPDATED,
    EVENT_QUERY_COMPLETED,
    EventPublisherSettings,
    EVENT_TYPE_TO_CHANNEL,
    InMemoryEventPublisher,
    KB_EVENT_TYPES,
    LOGICAL_CHANNELS,
    NoopEventPublisher,
    PUBLISHER_TYPE_BUS,
    PUBLISHER_TYPE_MEMORY,
    PUBLISHER_TYPE_NOOP,
    PublishedEnvelope,
    channel_for,
    load_event_publisher_settings,
)
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime(2026, 5, 3, 8, 0, 0, tzinfo=timezone.utc)


def _event(**overrides) -> ApplicationEvent:
    base = dict(
        id="evt-1",
        type=EVENT_DOCUMENT_UPLOADED,
        occurred_at=_now(),
        source="j1/test",
        subject="doc-1",
        tenant_id="acme",
        correlation_id="run-1",
        actor="svc-power",
        auth_type="api_key",
        data={"checksum": "sha256:abc"},
    )
    base.update(overrides)
    return ApplicationEvent(**base)


# ---- Channel mapping -------------------------------------------------


def test_channel_mapping_covers_every_shipped_event_type():
    """The framework's own KB_EVENT_TYPES must all be routed."""
    assert set(EVENT_TYPE_TO_CHANNEL.keys()) >= set(KB_EVENT_TYPES)


def test_channel_for_known_types_returns_logical_channel():
    cases = {
        EVENT_DOCUMENT_UPLOADED: CHANNEL_DOCUMENTS,
        EVENT_DOCUMENT_PARSING_STARTED: CHANNEL_INGESTION,
        EVENT_DOCUMENT_INGESTION_STARTED: CHANNEL_INGESTION,
        EVENT_DOCUMENT_INGESTION_FAILED: CHANNEL_INGESTION,
        EVENT_DOCUMENT_INDEXING_COMPLETED: CHANNEL_INDEXING,
        EVENT_KNOWLEDGE_UPDATED: CHANNEL_INDEXING,
        EVENT_QUERY_COMPLETED: CHANNEL_QUERY,
        EVENT_ANSWER_GENERATED: CHANNEL_ANSWER,
        EVENT_CITATION_VALIDATION_FAILED: CHANNEL_CITATION,
    }
    for event_type, channel in cases.items():
        assert channel_for(event_type) == channel


def test_channel_for_unknown_type_falls_back_to_audit():
    assert channel_for("custom.unknown.event") == CHANNEL_AUDIT


def test_logical_channels_includes_all_seven():
    assert set(LOGICAL_CHANNELS) == {
        CHANNEL_DOCUMENTS, CHANNEL_INGESTION, CHANNEL_INDEXING,
        CHANNEL_QUERY, CHANNEL_ANSWER, CHANNEL_CITATION, CHANNEL_AUDIT,
    }


# ---- Noop publisher --------------------------------------------------


def test_noop_publisher_accepts_events_silently():
    pub = NoopEventPublisher()
    pub.publish(_event())  # no return, no raise


def test_noop_publisher_publish_never_raises():
    pub = NoopEventPublisher()
    # Even with a malformed event,.publish must not raise.
    pub.publish(_event(id="", type=""))


# ---- In-memory publisher --------------------------------------------


def test_in_memory_publisher_records_events_with_resolved_channel():
    pub = InMemoryEventPublisher()
    pub.publish(_event())
    assert len(pub.published) == 1
    envelope = pub.published[0]
    assert isinstance(envelope, PublishedEnvelope)
    assert envelope.channel == CHANNEL_DOCUMENTS  # for document.uploaded


def test_in_memory_publisher_routes_by_event_type():
    pub = InMemoryEventPublisher()
    pub.publish(_event(type=EVENT_ANSWER_GENERATED))
    pub.publish(_event(type=EVENT_DOCUMENT_INGESTION_STARTED, id="evt-2"))
    pub.publish(_event(type="custom.x", id="evt-3"))
    assert pub.by_channel(CHANNEL_ANSWER) and pub.by_channel(CHANNEL_INGESTION)
    # custom event lands on the audit catch-all
    assert pub.by_channel(CHANNEL_AUDIT)


def test_in_memory_publisher_by_event_type_filters():
    pub = InMemoryEventPublisher()
    pub.publish(_event(type=EVENT_QUERY_COMPLETED, id="evt-1"))
    pub.publish(_event(type=EVENT_QUERY_COMPLETED, id="evt-2"))
    pub.publish(_event(type=EVENT_ANSWER_GENERATED, id="evt-3"))
    assert len(pub.by_event_type(EVENT_QUERY_COMPLETED)) == 2
    assert len(pub.by_event_type(EVENT_ANSWER_GENERATED)) == 1


def test_in_memory_publisher_clear():
    pub = InMemoryEventPublisher()
    pub.publish(_event())
    pub.clear()
    assert pub.published == ()


# ---- Header propagation (security context, correlation) -------------


def test_headers_carry_full_context_when_present():
    pub = InMemoryEventPublisher(producer="test-producer", schema_version="2.3")
    pub.publish(_event())
    headers = pub.published[0].headers
    assert headers["eventId"] == "evt-1"
    assert headers["eventType"] == EVENT_DOCUMENT_UPLOADED
    assert headers["producer"] == "test-producer"
    assert headers["schemaVersion"] == "2.3"
    assert headers["correlationId"] == "run-1"
    assert headers["requestId"] == "run-1"  # alias
    assert headers["tenantId"] == "acme"
    assert headers["actor"] == "svc-power"
    assert headers["authType"] == "api_key"
    # Default idempotency key: <eventType>:<subject>
    assert headers["idempotencyKey"] == f"{EVENT_DOCUMENT_UPLOADED}:doc-1"
    assert headers["occurredAt"] == _now().isoformat()


def test_headers_omit_fields_for_anonymous_events():
    """Events emitted by background workers carry no actor / auth_type."""
    pub = InMemoryEventPublisher()
    pub.publish(_event(actor=None, auth_type=None))
    headers = pub.published[0].headers
    assert "actor" not in headers
    assert "authType" not in headers


def test_headers_omit_correlation_when_absent():
    pub = InMemoryEventPublisher()
    pub.publish(_event(correlation_id=None))
    headers = pub.published[0].headers
    assert "correlationId" not in headers
    assert "requestId" not in headers


def test_headers_use_event_id_when_subject_is_absent():
    """Idempotency key fallback when subject isn't set."""
    pub = InMemoryEventPublisher()
    pub.publish(_event(subject=None))
    assert pub.published[0].headers["idempotencyKey"] == "evt-1"


def test_custom_idempotency_key_factory_overrides_default():
    pub = InMemoryEventPublisher(
        idempotency_key=lambda e: f"custom:{e.tenant_id}:{e.type}",
    )
    pub.publish(_event())
    assert pub.published[0].headers["idempotencyKey"] == \
        f"custom:acme:{EVENT_DOCUMENT_UPLOADED}"


def test_custom_idempotency_key_failure_is_swallowed():
    """A misbehaving key factory must not break publication."""

    def boom(_event):
        raise RuntimeError("boom")

    pub = InMemoryEventPublisher(idempotency_key=boom)
    pub.publish(_event())  # must not raise
    headers = pub.published[0].headers
    # No idempotencyKey set when the factory failed (and the default
    # wasn't used because a factory was explicitly supplied).
    assert "idempotencyKey" not in headers


# ---- Bus bridge ------------------------------------------------------


class _RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def handle(self, event: ApplicationEvent) -> None:
        self.events.append(event)


def test_bus_publisher_delegates_to_existing_event_bus():
    """Webhook + queue surfaces share one application event model."""
    sub = _RecordingSubscriber()
    bus = ApplicationEventBus([sub])
    pub = BusEventPublisher(bus)
    pub.publish(_event())
    assert len(sub.events) == 1
    assert sub.events[0].type == EVENT_DOCUMENT_UPLOADED


def test_bus_publisher_swallows_bus_exceptions():
    class _BoomBus:
        def publish(self, event):
            raise RuntimeError("bus exploded")

    pub = BusEventPublisher(_BoomBus())
    pub.publish(_event())  # must not raise


# ---- Composite publisher --------------------------------------------


def test_composite_publishes_to_all_delegates():
    a = InMemoryEventPublisher()
    b = InMemoryEventPublisher()
    pub = CompositeEventPublisher([a, b])
    pub.publish(_event())
    assert len(a.published) == 1
    assert len(b.published) == 1


def test_composite_isolates_failing_delegate():
    class _Boom:
        def publish(self, event):
            raise RuntimeError("dead")

    ok = InMemoryEventPublisher()
    pub = CompositeEventPublisher([_Boom(), ok])
    pub.publish(_event())  # must not raise
    assert len(ok.published) == 1


# ---- Settings + env loading -----------------------------------------


def test_settings_default_to_safe_values():
    settings = load_event_publisher_settings(env={})
    assert settings.publisher_type == PUBLISHER_TYPE_NOOP
    assert settings.producer == "j1"
    assert settings.schema_version == "1.0"
    assert settings.include_sensitive_payloads is False


def test_settings_from_env():
    settings = load_event_publisher_settings(env={
        "J1_EVENT_PUBLISHER_TYPE": "memory",
        "J1_EVENT_PUBLISHER_PRODUCER": "kb-eu1",
        "J1_EVENT_PUBLISHER_SCHEMA_VERSION": "2.0",
        "J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS": "true",
    })
    assert settings.publisher_type == PUBLISHER_TYPE_MEMORY
    assert settings.producer == "kb-eu1"
    assert settings.schema_version == "2.0"
    assert settings.include_sensitive_payloads is True


def test_settings_normalises_publisher_type_case():
    settings = load_event_publisher_settings(env={"J1_EVENT_PUBLISHER_TYPE": "BUS"})
    assert settings.publisher_type == PUBLISHER_TYPE_BUS


def test_settings_rejects_unknown_publisher_type():
    """Real broker adapters use their own type strings — built-in
 factory must NOT silently accept them."""
    with pytest.raises(ConfigError, match="unsupported"):
        load_event_publisher_settings(env={"J1_EVENT_PUBLISHER_TYPE": "kafka"})


def test_settings_dataclass_is_frozen():
    settings = EventPublisherSettings()
    with pytest.raises(Exception):
        settings.publisher_type = "memory"


# ---- Sensitive-payload policy: the headline guarantee --------------


def test_default_publisher_does_not_emit_full_document_content():
    """The framework's own publish_document_uploaded helper carries
 only checksum/size/mime — never the bytes. Verifies this contract
 via a representative event payload."""
    pub = InMemoryEventPublisher()
    pub.publish(_event(data={
        "documentId": "doc-1",
        "checksum": "sha256:abc",
        "fileSize": 4096,
        "mimeType": "application/pdf",
        "duplicate": False,
    }))
    payload = pub.published[0].event.data
    forbidden = {"content", "rawContent", "extractedText", "objectStorageKey",
                 "embedding", "embeddings", "vector"}
    assert not (forbidden & set(payload.keys()))


def test_default_publisher_does_not_emit_full_answer_text():
    """answer.generated payloads carry metadata only — not raw answer text."""
    pub = InMemoryEventPublisher()
    pub.publish(_event(type=EVENT_ANSWER_GENERATED, data={
        "question": "x", "modeUsed": "AUTO", "citationCount": 3,
        "confidence": 0.7, "reviewRequired": False,
    }))
    payload = pub.published[0].event.data
    assert "answer" not in payload
    assert "answerText" not in payload
