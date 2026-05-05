from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException, Request

from j1.integration.security import (
    ANONYMOUS_CONTEXT,
    Authenticator,
    AuthenticationError,
    AuthorizationError,
    Credential,
    SecurityContext,
)

AUTHORIZATION_HEADER = "Authorization"
API_KEY_HEADER = "X-API-Key"

_BEARER_PREFIX = "bearer "


@dataclass(frozen=True)
class SecurityPolicy:
    """REST-side wrapper around an `Authenticator` plus public-path config.

    `authenticator=None` disables authentication entirely (anonymous mode).
    `anonymous_paths` are exempt from auth even when `authenticator` is set.
    """

    authenticator: Authenticator | None = None
    # Anonymous paths bypass authentication. The defaults cover health
    # checks AND the OpenAPI / Swagger UI surface — operators must be
    # able to load the docs page without pasting a token, and Swagger
    # itself fetches `/openapi.json` before the operator can use the
    # Authorize button.
    anonymous_paths: frozenset[str] = frozenset({
        "/health",
        "/version",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    })

    @property
    def enabled(self) -> bool:
        return self.authenticator is not None

    def is_anonymous_path(self, path: str) -> bool:
        return path in self.anonymous_paths


def extract_credential(request: Request) -> Credential | None:
    """Pull a credential off the wire. Returns None if no auth header present."""
    auth_header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if auth_header:
        if auth_header.lower().startswith(_BEARER_PREFIX):
            token = auth_header[len(_BEARER_PREFIX):].strip()
            if token:
                return Credential(scheme="bearer", token=token)
        # Reject anything else under Authorization rather than silently fall
        # through — opaque schemes belong in `X-API-Key`.
        return None
    api_key = request.headers.get(API_KEY_HEADER, "").strip()
    if api_key:
        return Credential(scheme="api_key", token=api_key)
    return None


def authenticate_request(
    request: Request, policy: SecurityPolicy
) -> SecurityContext:
    """Resolve a `SecurityContext` for the request per `policy`.

    Raises `HTTPException(401)` for missing/invalid credentials when auth is
    enabled. Returns `ANONYMOUS_CONTEXT` when auth is disabled or the path
    is configured anonymous.
    """
    request_id = getattr(request.state, "request_id", None)

    if not policy.enabled:
        return _with_request_id(ANONYMOUS_CONTEXT, request_id)

    if policy.is_anonymous_path(request.url.path):
        return _with_request_id(ANONYMOUS_CONTEXT, request_id)

    credential = extract_credential(request)
    if credential is None:
        raise HTTPException(
            status_code=401, detail="missing authentication credential"
        )
    try:
        ctx = policy.authenticator.authenticate(credential)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _with_request_id(ctx, request_id)


def require_scope(security: SecurityContext, scope: str) -> None:
    """Enforce that `security` carries `scope`. No-op for anonymous mode."""
    if security.is_anonymous:
        return
    if scope and not security.has_scope(scope):
        raise AuthorizationError(
            f"missing required scope {scope!r}", required_scope=scope
        )


def require_any_scope(
    security: SecurityContext, scopes: Iterable[str]
) -> None:
    if security.is_anonymous:
        return
    scope_list = list(scopes)
    if scope_list and not security.has_any_scope(scope_list):
        raise AuthorizationError(
            f"missing one of required scopes {scope_list!r}",
            required_scope=scope_list[0],
        )


def _with_request_id(
    ctx: SecurityContext, request_id: str | None
) -> SecurityContext:
    if request_id is None or ctx.request_id == request_id:
        return ctx
    return SecurityContext(
        subject=ctx.subject,
        tenant_id=ctx.tenant_id,
        scopes=ctx.scopes,
        auth_type=ctx.auth_type,
        request_id=request_id,
        metadata=ctx.metadata,
    )
