"""Cross-layer consistency guard for the external integration surface.

These tests are deliberately structural — they don't exercise behaviour
(other suites do that). They assert that the *contracts* across the
external-integration layers stay aligned:

 - Every framework-shipped event type (`KB_EVENT_TYPES`) has a logical
 channel in `EVENT_TYPE_TO_CHANNEL` AND a documented message in the
 AsyncAPI spec.
 - Every scope used by a REST route is one of the central
 `SCOPE_*` constants (no inline `"kb:..."` literals leak in).
 - Every error code raised by REST exception handlers is documented
 in either `docs/rest-api.md` or `docs/security.md`.
 - Public symbols (events, scopes, channels, publishers, bulk
 schemas) are exported through `j1.integration.__init__` and the
 top-level `j1.__init__`.

A failure here means a future change drifted the contract. Fix the
drift, don't widen this test.
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

import j1
import j1.integration as integration
from j1 import (
    DEFAULT_KB_SCOPES,
    EVENT_TYPE_TO_CHANNEL,
    KB_EVENT_TYPES,
    LOGICAL_CHANNELS,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "j1"
_DOCS = _REPO_ROOT / "docs"


# ---- Event types ↔ channel mapping ↔ AsyncAPI spec -----------------


def test_every_kb_event_type_routes_to_a_channel():
    """The publisher's channel mapping covers every shipped event type."""
    missing = set(KB_EVENT_TYPES) - set(EVENT_TYPE_TO_CHANNEL.keys())
    assert not missing, (
        "EVENT_TYPE_TO_CHANNEL is missing entries for shipped event types: "
        + ", ".join(sorted(missing))
    )


def test_every_channel_in_mapping_is_a_logical_channel():
    """Mapping values must come from LOGICAL_CHANNELS — no typos."""
    bad = {ch for ch in EVENT_TYPE_TO_CHANNEL.values() if ch not in LOGICAL_CHANNELS}
    assert not bad, f"channel mapping uses non-logical channel(s): {bad}"


def test_asyncapi_spec_documents_every_event_type():
    spec_path = _DOCS / "asyncapi" / "kb-events.asyncapi.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    documented = set(spec["components"]["messages"].keys())
    expected = {_event_type_to_message_name(t) for t in KB_EVENT_TYPES}
    missing = expected - documented
    assert not missing, (
        "AsyncAPI spec missing message definitions for: " + ", ".join(sorted(missing))
    )


def _event_type_to_message_name(event_type: str) -> str:
    """`document.uploaded` → `DocumentUploaded`."""
    parts = event_type.replace(".", "_").split("_")
    return "".join(p.title() for p in parts)


# ---- Scope-constant discipline -------------------------------------


def test_no_inline_scope_literals_in_rest_routes():
    """Every `scope_required(...)` in REST must take a `SCOPE_*` constant."""
    text = (_SRC / "adapters" / "rest" / "app.py").read_text(encoding="utf-8")
    inline = re.findall(r'scope_required\(\s*"(kb:[^"]+)"\s*\)', text)
    assert not inline, (
        "REST routes must use the SCOPE_* constants (found inline literals: "
        + ", ".join(sorted(set(inline))) + ")"
    )


def test_default_kb_scopes_matches_individual_constants():
    """`DEFAULT_KB_SCOPES` is the union of every `SCOPE_*` constant — keep them in sync."""
    individual = {
        v for k, v in vars(j1).items()
        if k.startswith("SCOPE_") and isinstance(v, str)
    }
    assert individual == set(DEFAULT_KB_SCOPES), (
        f"DEFAULT_KB_SCOPES ({set(DEFAULT_KB_SCOPES)}) drifted from "
        f"SCOPE_* constants ({individual})"
    )


# ---- Error-code documentation --------------------------------------


def test_rest_error_codes_are_documented():
    """Every error code raised by a REST exception handler must appear in the docs."""
    app_text = (_SRC / "adapters" / "rest" / "app.py").read_text(encoding="utf-8")
    used_codes = set(re.findall(r'code="([A-Z_]+)"', app_text))
    rest_doc = (_DOCS / "rest-api.md").read_text(encoding="utf-8")
    security_doc = (_DOCS / "security.md").read_text(encoding="utf-8")
    documented_codes_text = rest_doc + "\n" + security_doc
    undocumented = {c for c in used_codes if c not in documented_codes_text}
    # `HTTP_xxx` is templated — match by prefix instead of exact string
    undocumented = {c for c in undocumented if not c.startswith("HTTP_")}
    assert not undocumented, (
        "REST error codes missing from docs: " + ", ".join(sorted(undocumented))
    )


# ---- Public-API completeness ---------------------------------------


def test_event_constants_exported_through_integration():
    """All KB_EVENT_TYPES constants must be importable from j1.integration."""
    integration_all = set(integration.__all__)
    expected = {
        f"EVENT_{t.upper().replace('.', '_')}"
        for t in KB_EVENT_TYPES
    }
    missing = expected - integration_all
    assert not missing, "missing from j1.integration.__all__: " + ", ".join(sorted(missing))


def test_event_constants_exported_through_top_level():
    j1_all = set(j1.__all__)
    expected = {
        f"EVENT_{t.upper().replace('.', '_')}"
        for t in KB_EVENT_TYPES
    }
    missing = expected - j1_all
    assert not missing, "missing from j1.__all__: " + ", ".join(sorted(missing))


@pytest.mark.parametrize("symbol", [
    # Publisher abstraction
    "EventPublisher", "NoopEventPublisher", "InMemoryEventPublisher",
    "BusEventPublisher", "CompositeEventPublisher", "PublishedEnvelope",
    "EventPublisherSettings", "load_event_publisher_settings",
    "channel_for", "EVENT_TYPE_TO_CHANNEL", "LOGICAL_CHANNELS",
    # Channels
    "CHANNEL_DOCUMENTS", "CHANNEL_INGESTION", "CHANNEL_INDEXING",
    "CHANNEL_QUERY", "CHANNEL_ANSWER", "CHANNEL_CITATION", "CHANNEL_AUDIT",
    # Webhook delivery
    "ApplicationEventBus", "WebhookSubscription",
    "StaticWebhookSubscriptionRegistry", "WebhookDeliveryRecord",
    "JsonlWebhookDeliveryStore", "InMemoryWebhookDeliveryStore",
    # Streaming (SSE)
    "AnswerStreamEvent", "AnswerStreamingService", "BufferingStreamHandler",
    "STREAM_EVENT_ANSWER_STARTED", "STREAM_EVENT_ANSWER_COMPLETED",
    "STREAM_EVENT_ANSWER_FAILED",
    # Bulk
    "BulkExportService", "BulkImportService", "BulkImportResult",
    "DocumentExportRecord", "ArtifactExportRecord", "FeedbackExportRecord",
    # Security
    "SecurityContext", "ApiKeyAuthenticator", "JwtAuthenticator",
    "AuthenticationError", "AuthorizationError",
    # CloudEvents
    "to_cloudevent", "sign_payload", "verify_signature",
])
def test_public_symbol_exported_at_top_level(symbol):
    assert symbol in j1.__all__, f"{symbol!r} missing from j1.__all__"
    assert hasattr(j1, symbol), f"{symbol!r} not importable from j1"


# ---- Architecture boundary (catches new leaks) ---------------------


def test_no_outer_layer_imports_in_core_subpackages():
    """`j1.intake`, `j1.processing`, `j1.query`, `j1.search`, `j1.artifacts`,
 `j1.audit`, `j1.cost`, `j1.review`, `j1.connectors`, `j1.enrichers`,
 `j1.workspace` MUST NOT import `j1.integration.*` or `j1.adapters.*`.
 """
    forbidden_prefixes = ("j1.integration", "j1.adapters")
    core_packages = {
        "intake", "processing", "query", "search", "artifacts",
        "audit", "cost", "review", "connectors", "enrichers",
        "workspace", "jobs", "documents",
    }
    offenders: list[tuple[str, str]] = []
    for py in _SRC.rglob("*.py"):
        rel = py.relative_to(_SRC)
        if rel.parts[0] not in core_packages:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            for mod in modules:
                for forbidden in forbidden_prefixes:
                    if mod == forbidden or mod.startswith(forbidden + "."):
                        offenders.append((str(rel), mod))
    assert not offenders, (
        "core packages must not import outer-layer modules:\n"
        + "\n".join(f"  {p}: imports {m}" for p, m in offenders)
    )


# ---- AsyncAPI spec ↔ logical channels round-trip -------------------


def test_asyncapi_channels_include_every_logical_channel():
    spec_path = _DOCS / "asyncapi" / "kb-events.asyncapi.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec_addresses = {c["address"] for c in spec["channels"].values()}
    missing = set(LOGICAL_CHANNELS) - spec_addresses
    assert not missing, (
        "AsyncAPI spec missing channel addresses: " + ", ".join(sorted(missing))
    )


# ---- CloudEvents extension naming aligned with publisher headers --


def test_cloudevents_extension_attrs_align_with_publisher_headers():
    """Webhook (CloudEvents) and queue (publisher) must agree on names.

 CloudEvents extension attribute names are lowercase letters/digits
 only (≤20 chars). The publisher headers use camelCase. The two sets
 must carry the same SEMANTIC fields so consumers can read either
 transport without branching.
 """
    from j1.integration.events.cloudevents import (
        EXTENSION_ACTOR, EXTENSION_AUTH_TYPE,
        EXTENSION_CORRELATION, EXTENSION_TENANT,
    )
    from j1.integration.events.publisher import _build_headers
    from j1.integration.events.event import ApplicationEvent
    from datetime import datetime, timezone

    event = ApplicationEvent(
        id="e", type="document.uploaded",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        tenant_id="acme", correlation_id="run-1",
        actor="svc", auth_type="api_key",
    )
    headers = _build_headers(event, producer="j1", schema_version="1.0")
    semantic_pairs = [
        ("tenantId", EXTENSION_TENANT),
        ("correlationId", EXTENSION_CORRELATION),
        ("actor", EXTENSION_ACTOR),
        ("authType", EXTENSION_AUTH_TYPE),
    ]
    for header_name, extension_name in semantic_pairs:
        assert header_name in headers, f"publisher header {header_name!r} not set"
        # The extension attr name is what CloudEvents consumers see.
        # Just sanity-check it's spec-compliant (lowercase letters/digits, ≤20 chars).
        assert extension_name.islower() and extension_name.isalnum()
        assert len(extension_name) <= 20
