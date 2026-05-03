# J1 Security Layer

Authentication and authorization for J1's external interfaces. The
boundary is **outer-layer**: HTTP headers, JWT objects, and OAuth state
never reach the application services. Adapters convert the wire-format
credential into a vendor-neutral [`SecurityContext`](../src/j1/integration/security/context.py)
and that is the only thing that flows inward.

---

## 1. Layering

```
HTTP request                  Outer adapter (j1.adapters.rest)
   │  Authorization / X-API-Key
   ▼
extract_credential(request) ──► Credential(scheme, token)
                                  │
                                  ▼
                          Authenticator.authenticate(...)
                                  │
                                  ▼
                          SecurityContext (subject, tenant_id,
                                            scopes, auth_type, …)
                                  │
                                  ▼
              FastAPI dependency: scope_required("kb:search")
                                  │            │
                                  │            └─► AuthorizationError → 403
                                  ▼
                          Route handler runs
                          (security context available)
```

What lives where:

| Module                                      | Purpose |
|---------------------------------------------|---------|
| [`j1.integration.security.context`](../src/j1/integration/security/context.py) | `SecurityContext`, `ANONYMOUS_CONTEXT`, auth-type constants |
| [`j1.integration.security.scopes`](../src/j1/integration/security/scopes.py)   | `kb:*` scope constants and `DEFAULT_KB_SCOPES` |
| [`j1.integration.security.errors`](../src/j1/integration/security/errors.py)   | `AuthenticationError`, `AuthorizationError` |
| [`j1.integration.security.authenticator`](../src/j1/integration/security/authenticator.py) | `Authenticator` Protocol, `Credential`, `ApiKeyAuthenticator`, `JwtAuthenticator`, `CompositeAuthenticator` |
| [`j1.integration.security.settings`](../src/j1/integration/security/settings.py) | `SecuritySettings`, `load_security_settings(env=...)` |
| [`j1.adapters.rest.security`](../src/j1/adapters/rest/security.py)             | REST-specific binding: header parsing, FastAPI deps, scope enforcement |

The integration layer carries no FastAPI / JWT-library imports. New
adapters (MCP, Webhook, gRPC, …) reuse the integration primitives and
implement their own header/transport mapping.

---

## 2. Supported authentication methods

### 2.1 API Key (implemented)

Two equivalent transports — pick whichever fits the client:

```http
Authorization: Bearer <opaque-token>
```
or
```http
X-API-Key: <opaque-token>
```

Tokens are opaque to the framework — they're looked up in an
`ApiKeyAuthenticator`'s in-memory map and resolved to an
[`ApiKeyRecord`](../src/j1/integration/security/authenticator.py):

```python
ApiKeyRecord(
    subject="svc-search-frontend",
    tenant_id="acme",
    scopes=frozenset({"kb:read", "kb:search", "kb:answer"}),
    metadata={"team": "search"},
)
```

The map's source is the deployment's concern: an env-injected JSON
blob, a file mounted from a secret manager, or a programmatic
construction at startup.

### 2.2 JWT / OAuth 2.1 / OIDC (placeholder)

[`JwtAuthenticator`](../src/j1/integration/security/authenticator.py)
ships as a vendor-neutral shell. It accepts an injected `verifier`
callable — the deployment plugs in PyJWT, Authlib, or whatever it
prefers — plus an optional `claims_mapper` for custom claim shapes:

```python
from j1 import JwtAuthenticator, CompositeAuthenticator, ApiKeyAuthenticator
import jwt  # PyJWT — chosen by the deployment, not by J1

def verify(token: str) -> dict:
    return jwt.decode(token, key=JWKS_KEY, algorithms=["RS256"], audience="kb")

auth = CompositeAuthenticator([
    JwtAuthenticator(verifier=verify),
    ApiKeyAuthenticator(API_KEYS),
])
```

The default claims mapper reads `sub`, `tenant_id` (or `tnt`), and
`scope` (space-separated string) or `scopes` (list). Override via the
`claims_mapper` argument for non-standard claim sets.

Until a verifier is configured, calling `JwtAuthenticator.authenticate`
raises `AuthenticationError("jwt authenticator has no verifier configured")` —
the framework refuses to silently accept unverified tokens.

### 2.3 Anonymous (default when `authenticator=None`)

When `create_rest_api` is built without an authenticator, every request
is treated as anonymous and scope checks are skipped. This keeps
local-development and integration-test setups simple. Production
deployments must pass an authenticator.

---

## 3. SecurityContext shape

```python
@dataclass(frozen=True)
class SecurityContext:
    subject:    str               # identity within the auth source
    tenant_id:  str | None
    scopes:     frozenset[str]
    auth_type:  str               # "anonymous" | "api_key" | "jwt"
    request_id: str | None = None
    metadata:   Mapping[str, str] = ...
```

`SecurityContext.is_anonymous` is the cheap predicate adapters and
helpers branch on. Everything is frozen — passing it inward is safe.

---

## 4. Scope catalog

| Scope            | Intent                                                |
|------------------|-------------------------------------------------------|
| `kb:read`        | Generic read of project state (documents, artifacts, citations, sources, reviews, capabilities) |
| `kb:search`      | Keyword search (`POST /search`)                       |
| `kb:retrieve`    | Context-block retrieval (`POST /retrieve`)            |
| `kb:answer`      | Generated answers (`POST /answer`)                    |
| `kb:ingest`      | Document upload, ingestion-job start                  |
| `kb:feedback`    | Submit user feedback                                  |
| `kb:admin`       | Project provisioning, workflow control, review decisions |
| `kb:audit.read`  | Audit-log + cost reports                              |
| `kb:delete`      | Reserved (no endpoints currently bound)               |

The complete set is exported as `DEFAULT_KB_SCOPES`. Custom scope
strings are allowed in tokens — the framework only checks for
membership.

---

## 5. Endpoint protection map

| Endpoint                                    | Required scope         |
|---------------------------------------------|------------------------|
| `GET /health`                               | (public)               |
| `GET /version`                              | (public)               |
| `GET /capabilities`                         | `kb:read`              |
| `POST /projects`                            | `kb:admin`             |
| `POST /documents`                           | `kb:ingest`            |
| `GET /documents/{id}`                       | `kb:read`              |
| `POST /documents/{id}/ingest`               | `kb:ingest`            |
| `GET /documents/{id}/status`                | `kb:read`              |
| `POST /ingestion-jobs`                      | `kb:ingest`            |
| `GET /ingestion-jobs/{id}`                  | `kb:read`              |
| `GET /ingestion-jobs/{id}/events`           | `kb:audit.read`        |
| `POST /ingestion-jobs/{id}/{pause,resume,cancel}` | `kb:admin`       |
| `GET /artifacts`                            | `kb:read`              |
| `GET /artifacts/{id}`                       | `kb:read`              |
| `POST /search`                              | `kb:search`            |
| `POST /retrieve`                            | `kb:retrieve`          |
| `POST /answer`                              | `kb:answer`            |
| `GET /citations/{id}`                       | `kb:read`              |
| `GET /sources/{id}`                         | `kb:read`              |
| `GET /cost`                                 | `kb:audit.read`        |
| `GET /reviews`                              | `kb:read`              |
| `POST /reviews/{id}/decision`               | `kb:admin`             |
| `POST /feedback`                            | `kb:feedback`          |

---

## 6. Error responses

The standard envelope (`{requestId, error: {code, message, details}}`)
is preserved for every auth failure. The `X-Request-Id` header is
set even on early-exit responses.

| Code                   | HTTP | When                                          |
|------------------------|------|-----------------------------------------------|
| `UNAUTHENTICATED`      | 401  | Missing or invalid credential                 |
| `INSUFFICIENT_SCOPE`   | 403  | Authenticated, but missing the required scope (`details.required_scope` is set) |
| `INVALID_IDENTIFIER`   | 400  | Bad tenant/project identifier (auth-adjacent) |
| `INVALID_ARGUMENT`     | 400  | `ValueError` (e.g. malformed body)            |
| `HTTP_500`             | 500  | Unhandled internal error                      |

Example:

```json
{
  "requestId": "9f1c…",
  "error": {
    "code": "INSUFFICIENT_SCOPE",
    "message": "missing required scope 'kb:ingest'",
    "details": { "required_scope": "kb:ingest" }
  }
}
```

---

## 7. Configuration

All knobs are read from the process environment via
[`load_security_settings`](../src/j1/integration/security/settings.py):

| Variable                    | Default        | Notes |
|-----------------------------|----------------|-------|
| `J1_AUTH_REQUIRED`          | `false`        | Truthy values: `1`, `true`, `yes`, `on`. Currently advisory — adapters opt in by passing `authenticator=...`. |
| `J1_AUTH_API_KEYS`          | _(unset)_      | Inline JSON object (`{"<token>": {"subject": "...", "tenant_id": "...", "scopes": [...]}}`). Convenient for local dev; **not** for production secrets. |
| `J1_AUTH_API_KEYS_FILE`     | _(unset)_      | Path to a JSON file with the same shape. Mount from a secret manager. Mutually exclusive with `J1_AUTH_API_KEYS`. |
| `J1_AUTH_JWT_ENABLED`       | `false`        | Flag for downstream wiring (the verifier itself is application code). |
| `J1_AUTH_ANONYMOUS_PATHS`   | `/health,/version` | Comma-separated paths exempt from auth. |
| `J1_AUTH_DEFAULT_TENANT_ID` | _(unset)_      | Optional fallback tenant for callers without one in their token. |

Real secrets must not live in the repository. The recommended pattern is
to mount a JSON file from your secrets store and point
`J1_AUTH_API_KEYS_FILE` at it.

---

## 8. Wiring example

```python
from j1 import (
    ApiKeyAuthenticator, ApiKeyRecord,
    SCOPE_READ, SCOPE_SEARCH, SCOPE_ANSWER,
    create_rest_api,
)

authenticator = ApiKeyAuthenticator({
    "kb_local_dev_001": ApiKeyRecord(
        subject="dev-laptop",
        tenant_id="acme",
        scopes=frozenset({SCOPE_READ, SCOPE_SEARCH, SCOPE_ANSWER}),
    ),
})

app = create_rest_api(
    facade,
    authenticator=authenticator,
    workspace=workspace,
)
```

Loaded from env instead:

```python
from j1 import load_security_settings, ApiKeyAuthenticator

settings = load_security_settings()  # reads J1_AUTH_*
authenticator = ApiKeyAuthenticator(settings.api_keys) if settings.api_keys else None

app = create_rest_api(
    facade,
    authenticator=authenticator,
    anonymous_paths=settings.anonymous_paths,
)
```

---

## 9. Example requests

```bash
# Authenticated search via Bearer token
curl -X POST http://localhost:8000/search \
  -H "Authorization: Bearer kb_local_dev_001" \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"query": "schedule"}'

# Same, via X-API-Key
curl -X POST http://localhost:8000/search \
  -H "X-API-Key: kb_local_dev_001" \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"query": "schedule"}'

# Public — no auth needed
curl http://localhost:8000/health
```

---

## 10. Future OAuth / OIDC path

When a deployment is ready to take OIDC, the path is:

1. Pick a JWT library (PyJWT, Authlib, …) — that lives in the
   deployment's wiring code, never in the framework.
2. Implement a `verifier(token: str) -> dict` that does signature
   verification, audience / issuer checks, and JWKS rotation.
3. Pass it to `JwtAuthenticator(verifier=verify)`.
4. Optionally wrap with `CompositeAuthenticator([jwt_auth, api_key_auth])`
   to keep machine-to-machine API keys working in parallel.
5. Map your IdP's claims to scopes (default mapper: `scope` claim,
   space-separated; or supply a custom `claims_mapper`).

The route surface, scope catalog, and error envelopes don't change.

---

## 11. Bridge to webhooks / CloudEvents

The REST adapter accepts an optional `event_bus: ApplicationEventBus`.
When wired, protected handlers (`POST /documents`, `POST /documents/{id}/ingest`,
`POST /ingestion-jobs`, `POST /search`, `POST /retrieve`, `POST /answer`)
publish an `ApplicationEvent` after the operation succeeds, carrying:

- `actor` = `SecurityContext.subject` (or `None` for anonymous calls)
- `auth_type` = `"api_key"` / `"jwt"` / `"anonymous"`
- `tenant_id` from the project context
- `correlation_id` = the `X-Request-Id` round-tripped on the response

Those values flow through to the CloudEvents payload as the
`kbactor` / `kbauthtype` extension attributes — receivers can prove
which authenticated identity caused a webhook to fire. See
[webhooks.md](webhooks.md) for the wire format.

The bridge lives in
[`j1.adapters.rest.events`](../src/j1/adapters/rest/events.py); it traps
publication failures so a misbehaving bus / subscriber cannot break an
HTTP response.

---

## 12. Constraints honoured

- **No domain-specific naming.** Scopes are `kb:*`; nothing in the
  layer references a customer or vertical.
- **No phase naming.** No `phase1`, `step2`, `spike`, etc.
- **No business logic in middleware.** The middleware does
  authentication and request-state setup only. Authorization decisions
  are scope membership checks.
- **No hardcoded customer / tenant.** Default tenant is configurable
  but never assumed.
- **No auth-library dependency in core.** `j1.integration.security`
  imports nothing outside the standard library + `j1.errors`. The REST
  binding imports FastAPI; that's the adapter's concern.
- **Secrets stay out of code.** Keys are loaded from env / files at
  deployment time; the framework ships no credentials.
