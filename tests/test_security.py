"""Tests for j1.integration.security primitives.

Covers context immutability, scope helpers, ApiKeyAuthenticator,
CompositeAuthenticator fallback, JwtAuthenticator placeholder behaviour,
and SecuritySettings env loading.
"""

import json
from pathlib import Path

import pytest

from j1.errors.exceptions import ConfigError
from j1.integration.security import (
    ANONYMOUS_CONTEXT,
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_JWT,
    DEFAULT_KB_SCOPES,
    SCOPE_ADMIN,
    SCOPE_ANSWER,
    SCOPE_AUDIT_READ,
    SCOPE_DELETE,
    SCOPE_FEEDBACK,
    SCOPE_INGEST,
    SCOPE_READ,
    SCOPE_RETRIEVE,
    SCOPE_SEARCH,
    SCOPE_VALIDATION_READ,
    SCOPE_VALIDATION_WRITE,
    ApiKeyAuthenticator,
    ApiKeyRecord,
    AuthenticationError,
    CompositeAuthenticator,
    Credential,
    JwtAuthenticator,
    SecurityContext,
    SecuritySettings,
    load_security_settings,
)


# ---- SecurityContext -------------------------------------------------


def test_anonymous_context_has_no_scopes():
    assert ANONYMOUS_CONTEXT.is_anonymous
    assert ANONYMOUS_CONTEXT.scopes == frozenset()
    assert ANONYMOUS_CONTEXT.tenant_id is None


def test_security_context_is_frozen():
    ctx = SecurityContext(subject="alice", tenant_id="acme")
    with pytest.raises(Exception):
        ctx.subject = "bob"


def test_has_scope_returns_true_for_present_scope():
    ctx = SecurityContext(
        subject="alice", tenant_id="acme",
        scopes=frozenset({SCOPE_READ, SCOPE_SEARCH}),
    )
    assert ctx.has_scope(SCOPE_READ)
    assert not ctx.has_scope(SCOPE_INGEST)


def test_has_any_scope_short_circuits():
    ctx = SecurityContext(
        subject="alice", tenant_id="acme",
        scopes=frozenset({SCOPE_READ}),
    )
    assert ctx.has_any_scope([SCOPE_INGEST, SCOPE_READ])
    assert not ctx.has_any_scope([SCOPE_INGEST, SCOPE_ADMIN])


# ---- Scope catalog ---------------------------------------------------


def test_default_kb_scopes_contains_all_named_scopes():
    expected = {
        SCOPE_READ, SCOPE_SEARCH, SCOPE_RETRIEVE, SCOPE_ANSWER,
        SCOPE_INGEST, SCOPE_FEEDBACK, SCOPE_ADMIN, SCOPE_DELETE,
        SCOPE_AUDIT_READ,
        SCOPE_VALIDATION_READ, SCOPE_VALIDATION_WRITE,
    }
    assert expected == set(DEFAULT_KB_SCOPES)


def test_scope_strings_use_kb_namespace():
    for scope in DEFAULT_KB_SCOPES:
        assert scope.startswith("kb:"), scope


# ---- ApiKeyAuthenticator ---------------------------------------------


def test_api_key_authenticator_returns_context_for_known_key():
    auth = ApiKeyAuthenticator({
        "tok-123": ApiKeyRecord(
            subject="svc-foo",
            tenant_id="acme",
            scopes=frozenset({SCOPE_READ, SCOPE_SEARCH}),
        ),
    })
    ctx = auth.authenticate(Credential(scheme="bearer", token="tok-123"))
    assert ctx.subject == "svc-foo"
    assert ctx.tenant_id == "acme"
    assert ctx.scopes == frozenset({SCOPE_READ, SCOPE_SEARCH})
    assert ctx.auth_type == AUTH_TYPE_API_KEY


def test_api_key_authenticator_accepts_api_key_scheme():
    auth = ApiKeyAuthenticator({
        "tok": ApiKeyRecord(subject="s", tenant_id="t"),
    })
    ctx = auth.authenticate(Credential(scheme="api_key", token="tok"))
    assert ctx.subject == "s"


def test_api_key_authenticator_rejects_unknown_key():
    auth = ApiKeyAuthenticator({})
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credential(scheme="bearer", token="nope"))


def test_api_key_authenticator_rejects_unsupported_scheme():
    auth = ApiKeyAuthenticator({"x": ApiKeyRecord(subject="s", tenant_id="t")})
    with pytest.raises(AuthenticationError, match="scheme"):
        auth.authenticate(Credential(scheme="basic", token="x"))


def test_api_key_authenticator_defensive_copy():
    """Mutating the source dict after construction must not affect lookups."""
    src: dict[str, ApiKeyRecord] = {"x": ApiKeyRecord(subject="s", tenant_id="t")}
    auth = ApiKeyAuthenticator(src)
    src["x"] = ApiKeyRecord(subject="evil", tenant_id="other")
    ctx = auth.authenticate(Credential(scheme="bearer", token="x"))
    assert ctx.subject == "s"


# ---- CompositeAuthenticator ------------------------------------------


def test_composite_returns_first_successful_match():
    a = ApiKeyAuthenticator({"a": ApiKeyRecord(subject="from-a", tenant_id="t")})
    b = ApiKeyAuthenticator({"b": ApiKeyRecord(subject="from-b", tenant_id="t")})
    composite = CompositeAuthenticator([a, b])
    assert composite.authenticate(
        Credential(scheme="bearer", token="b")
    ).subject == "from-b"


def test_composite_propagates_last_failure():
    composite = CompositeAuthenticator([
        ApiKeyAuthenticator({}),
        ApiKeyAuthenticator({}),
    ])
    with pytest.raises(AuthenticationError):
        composite.authenticate(Credential(scheme="bearer", token="x"))


def test_composite_requires_at_least_one_delegate():
    with pytest.raises(ValueError):
        CompositeAuthenticator([])


# ---- JwtAuthenticator ------------------------------------------------


def test_jwt_authenticator_without_verifier_rejects():
    auth = JwtAuthenticator()
    with pytest.raises(AuthenticationError, match="verifier"):
        auth.authenticate(Credential(scheme="bearer", token="anything"))


def test_jwt_authenticator_default_claims_mapper():
    def verifier(token: str) -> dict:
        return {"sub": "alice", "tenant_id": "acme", "scope": "kb:read kb:search"}

    auth = JwtAuthenticator(verifier=verifier)
    ctx = auth.authenticate(Credential(scheme="bearer", token="opaque"))
    assert ctx.subject == "alice"
    assert ctx.tenant_id == "acme"
    assert ctx.scopes == frozenset({SCOPE_READ, SCOPE_SEARCH})
    assert ctx.auth_type == AUTH_TYPE_JWT


def test_jwt_authenticator_translates_verifier_errors():
    def verifier(token: str):
        raise RuntimeError("expired")

    auth = JwtAuthenticator(verifier=verifier)
    with pytest.raises(AuthenticationError, match="expired"):
        auth.authenticate(Credential(scheme="bearer", token="x"))


def test_jwt_authenticator_rejects_non_bearer():
    auth = JwtAuthenticator(verifier=lambda t: {"sub": "x"})
    with pytest.raises(AuthenticationError, match="scheme"):
        auth.authenticate(Credential(scheme="api_key", token="x"))


# ---- SecuritySettings ------------------------------------------------


def test_load_security_settings_defaults_to_disabled():
    settings = load_security_settings(env={})
    assert settings.auth_required is False
    assert settings.api_keys == {}
    assert settings.jwt_enabled is False
    assert "/health" in settings.anonymous_paths
    assert "/version" in settings.anonymous_paths


def test_load_security_settings_reads_required_flag():
    settings = load_security_settings(env={"J1_AUTH_REQUIRED": "true"})
    assert settings.auth_required is True


def test_load_security_settings_inline_keys():
    raw = json.dumps({
        "tok-1": {
            "subject": "svc",
            "tenant_id": "acme",
            "scopes": ["kb:read", "kb:search"],
        }
    })
    settings = load_security_settings(env={"J1_AUTH_API_KEYS": raw})
    assert "tok-1" in settings.api_keys
    record = settings.api_keys["tok-1"]
    assert record.subject == "svc"
    assert record.scopes == frozenset({"kb:read", "kb:search"})


def test_load_security_settings_keys_file(tmp_path: Path):
    file = tmp_path / "keys.json"
    file.write_text(json.dumps({
        "tok-2": {"subject": "svc-2", "tenant_id": "acme", "scopes": []}
    }))
    settings = load_security_settings(
        env={"J1_AUTH_API_KEYS_FILE": str(file)}
    )
    assert "tok-2" in settings.api_keys


def test_load_security_settings_rejects_both_inline_and_file(tmp_path: Path):
    file = tmp_path / "keys.json"
    file.write_text("{}")
    with pytest.raises(ConfigError, match="only one"):
        load_security_settings(env={
            "J1_AUTH_API_KEYS": "{}",
            "J1_AUTH_API_KEYS_FILE": str(file),
        })


def test_load_security_settings_rejects_invalid_json():
    with pytest.raises(ConfigError, match="JSON"):
        load_security_settings(env={"J1_AUTH_API_KEYS": "not json"})


def test_load_security_settings_rejects_missing_subject():
    raw = json.dumps({"tok": {"tenant_id": "acme"}})
    with pytest.raises(ConfigError, match="subject"):
        load_security_settings(env={"J1_AUTH_API_KEYS": raw})


def test_load_security_settings_anonymous_paths_override():
    settings = load_security_settings(env={
        "J1_AUTH_ANONYMOUS_PATHS": "/health,/openapi.json,/docs",
    })
    assert settings.anonymous_paths == frozenset({
        "/health", "/openapi.json", "/docs",
    })


def test_load_security_settings_default_tenant_id():
    settings = load_security_settings(env={"J1_AUTH_DEFAULT_TENANT_ID": "acme"})
    assert settings.default_tenant_id == "acme"


def test_load_security_settings_jwt_flag():
    settings = load_security_settings(env={"J1_AUTH_JWT_ENABLED": "1"})
    assert settings.jwt_enabled is True


def test_security_settings_is_frozen():
    settings = SecuritySettings()
    with pytest.raises(Exception):
        settings.auth_required = True
