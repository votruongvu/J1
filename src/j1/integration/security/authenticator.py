from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from j1.integration.security.context import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_JWT,
    SecurityContext,
)
from j1.integration.security.errors import AuthenticationError


@dataclass(frozen=True)
class Credential:
    """Wire-format-neutral credential carried from an adapter to authenticators.

    `scheme` mirrors the standard HTTP authentication schemes (e.g. `bearer`,
    `api_key`); `token` is the opaque secret. Adapters extract these from
    headers, query strings, or other transport-specific places.
    """

    scheme: str
    token: str


class Authenticator(Protocol):
    """Maps a `Credential` to a `SecurityContext` or rejects it.

    Implementations must be vendor-neutral and free of HTTP / JWT-library
    types — those belong in adapters. Callers should treat
    `AuthenticationError` as the only signalling channel for failure.
    """

    def authenticate(self, credential: Credential) -> SecurityContext: ...


@dataclass(frozen=True)
class ApiKeyRecord:
    subject: str
    tenant_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, str] = field(default_factory=dict)


class ApiKeyAuthenticator:
    """Validates opaque API keys against an in-memory map.

    The map's source (file, env, secrets manager) is the caller's concern.
    Recognised schemes: `bearer` and `api_key`. Other schemes are rejected
    so a chain via `CompositeAuthenticator` can fall through.
    """

    SUPPORTED_SCHEMES = frozenset({"bearer", "api_key"})

    def __init__(self, keys: Mapping[str, ApiKeyRecord]) -> None:
        # Defensive copy — caller may mutate their map after construction.
        self._keys: dict[str, ApiKeyRecord] = dict(keys)

    def authenticate(self, credential: Credential) -> SecurityContext:
        if credential.scheme.lower() not in self.SUPPORTED_SCHEMES:
            raise AuthenticationError(
                f"unsupported credential scheme {credential.scheme!r}"
            )
        record = self._keys.get(credential.token)
        if record is None:
            raise AuthenticationError("invalid api key")
        return SecurityContext(
            subject=record.subject,
            tenant_id=record.tenant_id,
            scopes=frozenset(record.scopes),
            auth_type=AUTH_TYPE_API_KEY,
            metadata=dict(record.metadata),
        )


class JwtAuthenticator:
    """Placeholder for a future JWT / OAuth 2.1 / OIDC authenticator.

    The concrete verifier (signature check, JWKS fetch, claim mapping) is
    injected so the integration layer never imports a JWT library directly.
    A `verifier` callable returns the validated claims as a plain dict;
    `claims_mapper` converts those claims into a `SecurityContext`.

    Until a deployment configures a verifier, calling `authenticate` raises
    `AuthenticationError` — the framework ships the protocol but no default
    crypto.
    """

    def __init__(
        self,
        verifier=None,
        claims_mapper=None,
    ) -> None:
        self._verifier = verifier
        self._claims_mapper = claims_mapper or _default_jwt_claims_mapper

    def authenticate(self, credential: Credential) -> SecurityContext:
        if credential.scheme.lower() != "bearer":
            raise AuthenticationError(
                f"unsupported credential scheme {credential.scheme!r}"
            )
        if self._verifier is None:
            raise AuthenticationError(
                "jwt authenticator has no verifier configured"
            )
        try:
            claims = self._verifier(credential.token)
        except Exception as exc:
            raise AuthenticationError(f"jwt verification failed: {exc}") from exc
        return self._claims_mapper(claims)


def _default_jwt_claims_mapper(claims: Mapping[str, object]) -> SecurityContext:
    subject = str(claims.get("sub") or "")
    if not subject:
        raise AuthenticationError("jwt missing 'sub' claim")
    tenant = claims.get("tenant_id") or claims.get("tnt")
    raw_scopes = claims.get("scope") or claims.get("scopes") or ""
    if isinstance(raw_scopes, str):
        scopes = frozenset(s for s in raw_scopes.split() if s)
    else:
        scopes = frozenset(str(s) for s in raw_scopes)
    return SecurityContext(
        subject=subject,
        tenant_id=str(tenant) if tenant else None,
        scopes=scopes,
        auth_type=AUTH_TYPE_JWT,
    )


class CompositeAuthenticator:
    """Tries multiple authenticators in order; returns the first to succeed.

    Useful for accepting both API keys and JWTs at the same endpoint without
    coupling them. Raises the last `AuthenticationError` if all delegates
    reject — callers see one consolidated 401.
    """

    def __init__(self, delegates: list[Authenticator]) -> None:
        if not delegates:
            raise ValueError("CompositeAuthenticator requires at least one delegate")
        self._delegates = list(delegates)

    def authenticate(self, credential: Credential) -> SecurityContext:
        last_error: AuthenticationError | None = None
        for delegate in self._delegates:
            try:
                return delegate.authenticate(credential)
            except AuthenticationError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error
