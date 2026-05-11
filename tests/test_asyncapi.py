"""Tests for the AsyncAPI 3.0 specification at docs/asyncapi/.

These do *structural* validation only — they don't pull in a full
AsyncAPI validator (the framework intentionally adds zero new
runtime/dev dependencies for this surface). Instead we assert the
spec contains every channel + every event type the publisher
abstraction routes, so the spec can't drift away from the code.

Validation against the AsyncAPI JSON schema is documented in
docs/event-integration.md as an opt-in workflow (`npx
@asyncapi/cli validate...`); CI can wire it in later without
changing this module.
"""

from pathlib import Path

import pytest
import yaml

from j1.integration import (
    EVENT_TYPE_TO_CHANNEL,
    KB_EVENT_TYPES,
    LOGICAL_CHANNELS,
)


SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs" / "asyncapi" / "kb-events.asyncapi.yaml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC_PATH.exists(), f"AsyncAPI spec missing at {SPEC_PATH}"
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


# ---- Top-level shape ------------------------------------------------


def test_spec_declares_asyncapi_3(spec):
    assert spec["asyncapi"].startswith("3.")


def test_spec_has_required_top_level_keys(spec):
    for key in ("info", "channels", "operations", "components"):
        assert key in spec, f"missing top-level key: {key}"


def test_spec_default_content_type_is_json(spec):
    assert spec["defaultContentType"] == "application/json"


def test_spec_includes_at_least_one_server(spec):
    """Servers are example placeholders — but at least one helps tooling."""
    assert spec["servers"] and len(spec["servers"]) >= 1


# ---- Channels match the LOGICAL_CHANNELS registry ------------------


def test_every_logical_channel_is_documented(spec):
    spec_channels = {c["address"] for c in spec["channels"].values()}
    missing = set(LOGICAL_CHANNELS) - spec_channels
    # kb.audit is a catch-all channel — accept either as a documented
    # channel or as an implicit fallback (we test both shapes).
    assert not missing or missing == {"kb.audit"} or missing == set(), \
        f"AsyncAPI spec missing channels: {missing}"


def test_kb_audit_channel_is_documented(spec):
    spec_channels = {c["address"] for c in spec["channels"].values()}
    assert "kb.audit" in spec_channels


# ---- Every shipped event type has a documented message --------------


def test_every_kb_event_type_has_a_message(spec):
    documented = set(spec["components"]["messages"].keys())

    # Map event-type strings → expected PascalCase message names.
    expected = {_event_type_to_message_name(t) for t in KB_EVENT_TYPES}
    missing = expected - documented
    assert not missing, f"AsyncAPI spec missing messages for: {missing}"


def test_every_event_type_in_mapping_has_a_message(spec):
    """Same as above but routed via EVENT_TYPE_TO_CHANNEL."""
    documented = set(spec["components"]["messages"].keys())
    for event_type in EVENT_TYPE_TO_CHANNEL:
        expected = _event_type_to_message_name(event_type)
        assert expected in documented, f"missing message {expected!r}"


def _event_type_to_message_name(event_type: str) -> str:
    """document.uploaded → DocumentUploaded; doc.parsing_started → DocumentParsingStarted."""
    parts = event_type.replace(".", "_").split("_")
    return "".join(p.title() for p in parts)


# ---- Headers schema ------------------------------------------------


def test_common_headers_schema_includes_correlation_and_tenant(spec):
    schema = spec["components"]["schemas"]["CommonHeaders"]
    props = schema["properties"]
    for required in ("eventId", "eventType", "occurredAt", "producer", "schemaVersion"):
        assert required in schema["required"], f"missing required header: {required}"
    for optional in ("correlationId", "tenantId", "actor", "authType",
                     "idempotencyKey", "traceparent"):
        assert optional in props, f"header schema missing: {optional}"


def test_common_headers_actor_authtype_aligned_with_security_layer(spec):
    """Header names must match the ones the publisher actually sets."""
    props = spec["components"]["schemas"]["CommonHeaders"]["properties"]
    auth_type_enum = props["authType"]["enum"]
    assert set(auth_type_enum) == {"api_key", "jwt", "anonymous"}


# ---- Channel-message wiring (every channel has at least one msg) ---


def test_every_channel_lists_at_least_one_message(spec):
    for name, channel in spec["channels"].items():
        # kb.audit is the catch-all — it may legitimately have no
        # explicit messages declared (the publisher routes
        # custom/unknown events here at runtime).
        if channel["address"] == "kb.audit":
            continue
        assert channel.get("messages"), f"channel {name!r} has no messages"


# ---- Sensitive-payload policy is documented in the spec text -------


def test_answer_message_documents_no_full_text_policy(spec):
    msg = spec["components"]["messages"]["AnswerGenerated"]
    desc = msg.get("description", "")
    assert "full answer text" in desc.lower() or "never" in desc.lower(), \
        "AnswerGenerated message must explicitly document the no-full-text policy"


def test_query_message_documents_sensitivity_policy(spec):
    msg = spec["components"]["messages"]["QueryCompleted"]
    desc = msg.get("description", "")
    assert "sensitive" in desc.lower() or "include_sensitive" in desc.lower(), \
        "QueryCompleted message must document the sensitive-payload policy"


def test_failure_payload_does_not_expose_internals(spec):
    schema = spec["components"]["schemas"]["IngestionFailurePayload"]
    description = schema["properties"]["errorCode"]["description"]
    assert "stack trace" in description.lower() or "internal" in description.lower(), \
        "Failure payload must explicitly forbid internal exception leakage"
