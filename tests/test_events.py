"""Tests for j1.integration.events primitives.

Covers ApplicationEvent shape, ApplicationEventBus fan-out + isolation,
CloudEvents 1.0 mapping, signing/verification, subscription matching,
and WebhookSettings env loading.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.errors.exceptions import ConfigError
from j1.integration.events import (
    CLOUDEVENTS_SPEC_VERSION,
    DATA_CONTENT_TYPE,
    DEFAULT_EVENT_SOURCE,
    EVENT_ANSWER_GENERATED,
    EVENT_DOCUMENT_INDEXING_COMPLETED,
    EVENT_DOCUMENT_INDEXING_STARTED,
    EVENT_DOCUMENT_INGESTION_COMPLETED,
    EVENT_DOCUMENT_INGESTION_FAILED,
    EVENT_DOCUMENT_INGESTION_STARTED,
    EVENT_DOCUMENT_PARSING_COMPLETED,
    EVENT_DOCUMENT_PARSING_STARTED,
    EVENT_DOCUMENT_UPLOADED,
    EVENT_KNOWLEDGE_UPDATED,
    EVENT_QUERY_COMPLETED,
    EVENT_CITATION_VALIDATION_FAILED,
    KB_EVENT_TYPES,
    SIGNATURE_PREFIX,
    WILDCARD_EVENT_TYPE,
    ApplicationEvent,
    ApplicationEventBus,
    StaticWebhookSubscriptionRegistry,
    WebhookSubscription,
    load_webhook_settings,
    sign_payload,
    to_cloudevent,
    verify_signature,
)
from j1.integration.events.cloudevents import (
    EXTENSION_ACTOR,
    EXTENSION_AUTH_TYPE,
    EXTENSION_CORRELATION,
    EXTENSION_TENANT,
)


def _now() -> datetime:
    return datetime(2026, 5, 3, 8, 30, 21, tzinfo=timezone.utc)


def _event(**overrides) -> ApplicationEvent:
    base = dict(
        id="evt-1",
        type=EVENT_DOCUMENT_UPLOADED,
        occurred_at=_now(),
        source=DEFAULT_EVENT_SOURCE,
        subject="doc-1",
        tenant_id="acme",
        correlation_id="run-1",
        data={"checksum": "sha256:abc"},
    )
    base.update(overrides)
    return ApplicationEvent(**base)


# ---- ApplicationEvent ------------------------------------------------


def test_application_event_is_frozen():
    e = _event()
    with pytest.raises(Exception):
        e.id = "other"


def test_event_type_catalog_complete():
    expected = {
        EVENT_DOCUMENT_UPLOADED,
        EVENT_DOCUMENT_PARSING_STARTED,
        EVENT_DOCUMENT_PARSING_COMPLETED,
        EVENT_DOCUMENT_INGESTION_STARTED,
        EVENT_DOCUMENT_INGESTION_COMPLETED,
        EVENT_DOCUMENT_INGESTION_FAILED,
        EVENT_DOCUMENT_INDEXING_STARTED,
        EVENT_DOCUMENT_INDEXING_COMPLETED,
        EVENT_KNOWLEDGE_UPDATED,
        EVENT_QUERY_COMPLETED,
        EVENT_ANSWER_GENERATED,
        EVENT_CITATION_VALIDATION_FAILED,
    }
    assert expected == set(KB_EVENT_TYPES)


# ---- CloudEvents mapping --------------------------------------------


def test_to_cloudevent_minimal_envelope():
    envelope = to_cloudevent(_event())
    assert envelope["specversion"] == CLOUDEVENTS_SPEC_VERSION == "1.0"
    assert envelope["type"] == EVENT_DOCUMENT_UPLOADED
    assert envelope["source"] == DEFAULT_EVENT_SOURCE
    assert envelope["id"] == "evt-1"
    assert envelope["time"] == _now().isoformat()
    assert envelope["datacontenttype"] == DATA_CONTENT_TYPE
    assert envelope["subject"] == "doc-1"
    assert envelope["data"] == {"checksum": "sha256:abc"}


def test_to_cloudevent_carries_extension_attributes():
    envelope = to_cloudevent(_event())
    assert envelope[EXTENSION_TENANT] == "acme"
    assert envelope[EXTENSION_CORRELATION] == "run-1"


def test_to_cloudevent_omits_empty_optional_fields():
    """Per the spec, optional attributes SHOULD NOT be present when empty."""
    e = _event(
        subject=None, tenant_id=None, correlation_id=None,
        actor=None, auth_type=None,
    )
    envelope = to_cloudevent(e)
    assert "subject" not in envelope
    assert EXTENSION_TENANT not in envelope
    assert EXTENSION_CORRELATION not in envelope
    assert EXTENSION_ACTOR not in envelope
    assert EXTENSION_AUTH_TYPE not in envelope


def test_to_cloudevent_carries_actor_and_auth_type():
    e = _event(actor="svc-power", auth_type="api_key")
    envelope = to_cloudevent(e)
    assert envelope[EXTENSION_ACTOR] == "svc-power"
    assert envelope[EXTENSION_AUTH_TYPE] == "api_key"


def test_actor_extension_names_are_spec_compliant():
    for name in (EXTENSION_ACTOR, EXTENSION_AUTH_TYPE):
        assert name.islower() and name.isalnum() and len(name) <= 20


def test_cloudevent_extension_names_are_spec_compliant():
    """CloudEvents 1.0 extension attribute names: lowercase letters/digits only."""
    envelope = to_cloudevent(_event())
    for key in envelope:
        if key in {
            "specversion", "type", "source", "id", "time",
            "datacontenttype", "subject", "data",
        }:
            continue
        assert key.islower() and key.isalnum() and len(key) <= 20, key


# ---- ApplicationEventBus --------------------------------------------


class _RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def handle(self, event: ApplicationEvent) -> None:
        self.events.append(event)


class _RaisingSubscriber:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, event: ApplicationEvent) -> None:
        self.calls += 1
        raise RuntimeError("boom")


def test_bus_publishes_to_all_subscribers():
    a = _RecordingSubscriber()
    b = _RecordingSubscriber()
    bus = ApplicationEventBus([a, b])
    bus.publish(_event())
    assert len(a.events) == 1
    assert len(b.events) == 1


def test_bus_subscribe_after_construction():
    bus = ApplicationEventBus()
    sub = _RecordingSubscriber()
    bus.subscribe(sub)
    bus.publish(_event())
    assert sub.events[0].type == EVENT_DOCUMENT_UPLOADED


def test_bus_isolates_subscriber_failures():
    """A failing subscriber must not stop other subscribers from receiving."""
    failing = _RaisingSubscriber()
    ok = _RecordingSubscriber()
    bus = ApplicationEventBus([failing, ok])
    bus.publish(_event())
    assert failing.calls == 1
    assert len(ok.events) == 1


def test_bus_publish_never_raises():
    bus = ApplicationEventBus([_RaisingSubscriber()])
    bus.publish(_event())  # must not raise


# ---- Signing ---------------------------------------------------------


def test_sign_payload_uses_sha256_prefix():
    sig = sign_payload("topsecret", b"hello")
    assert sig.startswith(SIGNATURE_PREFIX)
    # 64 hex chars after the prefix
    assert len(sig) == len(SIGNATURE_PREFIX) + 64


def test_sign_payload_empty_secret_returns_empty():
    assert sign_payload("", b"hello") == ""


def test_signature_round_trip():
    payload = b'{"id":"evt-1"}'
    sig = sign_payload("topsecret", payload)
    assert verify_signature("topsecret", payload, sig) is True


def test_signature_rejects_tampered_payload():
    payload = b'{"id":"evt-1"}'
    sig = sign_payload("topsecret", payload)
    assert verify_signature("topsecret", payload + b"x", sig) is False


def test_signature_rejects_wrong_secret():
    payload = b'{"id":"evt-1"}'
    sig = sign_payload("topsecret", payload)
    assert verify_signature("wrongsecret", payload, sig) is False


def test_signature_rejects_empty_inputs():
    assert verify_signature("", b"x", "sha256=abc") is False
    assert verify_signature("s", b"x", "") is False


# ---- Subscription matching ------------------------------------------


def _sub(**overrides) -> WebhookSubscription:
    base = dict(
        id="sub-1",
        url="https://example.com/hook",
        event_types=frozenset({EVENT_DOCUMENT_UPLOADED}),
        secret="s",
    )
    base.update(overrides)
    return WebhookSubscription(**base)


def test_subscription_matches_event_type():
    sub = _sub()
    assert sub.accepts(_event())
    assert not sub.accepts(_event(type=EVENT_ANSWER_GENERATED))


def test_subscription_wildcard_matches_everything():
    sub = _sub(event_types=frozenset({WILDCARD_EVENT_TYPE}))
    assert sub.accepts(_event())
    assert sub.accepts(_event(type=EVENT_ANSWER_GENERATED))


def test_subscription_filters_by_tenant():
    sub = _sub(tenant_id="other")
    assert not sub.accepts(_event(tenant_id="acme"))
    assert sub.accepts(_event(tenant_id="other"))


def test_subscription_with_no_tenant_matches_any_tenant():
    sub = _sub(tenant_id=None)
    assert sub.accepts(_event(tenant_id="acme"))
    assert sub.accepts(_event(tenant_id=None))


def test_disabled_subscription_never_matches():
    sub = _sub(enabled=False)
    assert not sub.accepts(_event())


def test_static_registry_returns_only_matching():
    registry = StaticWebhookSubscriptionRegistry([
        _sub(id="match", event_types=frozenset({EVENT_DOCUMENT_UPLOADED})),
        _sub(id="other-tenant", tenant_id="other"),
        _sub(id="disabled", enabled=False),
        _sub(id="wildcard", event_types=frozenset({WILDCARD_EVENT_TYPE})),
    ])
    matches = registry.matching(_event())
    assert {s.id for s in matches} == {"match", "wildcard"}


# ---- WebhookSettings env loading ------------------------------------


def test_load_webhook_settings_defaults_to_disabled():
    settings = load_webhook_settings(env={})
    assert settings.enabled is False
    assert settings.subscriptions == ()
    assert settings.default_timeout_seconds == 10.0


def test_load_webhook_settings_inline_subscriptions():
    raw = json.dumps([{
        "id": "s1",
        "url": "https://example.com/hook",
        "event_types": ["document.uploaded", "answer.generated"],
        "secret": "topsecret",
        "tenant_id": "acme",
    }])
    settings = load_webhook_settings(env={
        "J1_WEBHOOK_ENABLED": "true",
        "J1_WEBHOOK_SUBSCRIPTIONS": raw,
    })
    assert settings.enabled is True
    assert len(settings.subscriptions) == 1
    sub = settings.subscriptions[0]
    assert sub.url == "https://example.com/hook"
    assert sub.tenant_id == "acme"
    assert sub.secret == "topsecret"
    assert "document.uploaded" in sub.event_types


def test_load_webhook_settings_from_file(tmp_path: Path):
    file = tmp_path / "subs.json"
    file.write_text(json.dumps([{
        "id": "s1", "url": "https://x", "event_types": ["*"],
    }]))
    settings = load_webhook_settings(env={
        "J1_WEBHOOK_SUBSCRIPTIONS_FILE": str(file),
    })
    assert len(settings.subscriptions) == 1


def test_load_webhook_settings_rejects_inline_and_file_together(tmp_path: Path):
    file = tmp_path / "x.json"
    file.write_text("[]")
    with pytest.raises(ConfigError, match="only one"):
        load_webhook_settings(env={
            "J1_WEBHOOK_SUBSCRIPTIONS": "[]",
            "J1_WEBHOOK_SUBSCRIPTIONS_FILE": str(file),
        })


def test_load_webhook_settings_rejects_missing_url():
    raw = json.dumps([{"id": "s1", "event_types": ["*"]}])
    with pytest.raises(ConfigError, match="url"):
        load_webhook_settings(env={"J1_WEBHOOK_SUBSCRIPTIONS": raw})


def test_load_webhook_settings_camelcase_keys_supported():
    raw = json.dumps([{
        "id": "s1", "url": "https://x",
        "eventTypes": ["document.uploaded"],
        "tenantId": "acme",
    }])
    settings = load_webhook_settings(env={"J1_WEBHOOK_SUBSCRIPTIONS": raw})
    sub = settings.subscriptions[0]
    assert "document.uploaded" in sub.event_types
    assert sub.tenant_id == "acme"


def test_load_webhook_settings_default_overrides():
    settings = load_webhook_settings(env={
        "J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS": "30",
        "J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS": "10",
    })
    assert settings.default_timeout_seconds == 30.0
    assert settings.default_max_attempts == 10
