# MCP (Model Context Protocol) Status

**Status: not implemented in this revision.**

The framework's external-integration architecture explicitly leaves
room for an MCP adapter (see
[external-integration-architecture.md](external-integration-architecture.md))
but no `j1.adapters.mcp` package ships today. This document records
the design constraints any future adapter must satisfy so an
implementation can land later without changing the integration layer.

---

## 1. Why deferred

- The framework's current public surface is REST + webhook + queue;
 MCP is a separate transport that re-uses the same application
 ports. The Protocol-driven design means MCP can land independently.
- Adding MCP today would require a `mcp` Python SDK dependency and a
 durable session/transport story (stdio, HTTP/SSE, WebSocket — the
 spec is in flux). Per the project's repeated constraint to "avoid
 large rewrites" and "no mandatory broker dependencies unless the
 repo already uses them", we keep MCP as a documented gap.
- The cross-cutting contracts MCP would consume (security context,
 scopes, ports, error envelope) are already in place — see § 3.

---

## 2. Required contracts an MCP adapter MUST honour

When the adapter lands, it MUST satisfy the same architectural rules
as the existing REST adapter:

1. Live in `j1.adapters.mcp/` — never inside `j1.integration.*` or
 any core package.
2. Take an `ApplicationFacade` and a `SecurityContext` resolver from
 the inbound transport. Never bypass the security layer.
3. Map MCP tool calls → port methods on `ApplicationFacade`. Never
 reach into `j1.processing.*`, `j1.query.*`, or other core
 packages directly.
4. Use the same `SCOPE_*` constants for authorization. No custom
 scope strings.
5. Emit failures via the same `AuthorizationError` /
 `AuthenticationError` types — translation to MCP error codes is
 the adapter's job.

The dependency-direction guards in
[`tests/test_external_integration_consistency.py`](../tests/test_external_integration_consistency.py)
will pick up violations automatically once the package exists.

---

## 3. Recommended scope mapping for MCP tools

The framework already centralises tool-grade scopes in
[`j1/integration/security/scopes.py`](../src/j1/integration/security/scopes.py).
The recommended mapping for MCP tools, mirroring the REST surface:

### Read-only tools (default-enabled, suitable for general agents)

| MCP tool name (suggested) | Backed by `ApplicationFacade.*` | Required scope |
|---|---|---|
| `kb.search` | `search.search(...)` | `kb:search` |
| `kb.retrieve` | `search.search(...)` (block view) | `kb:retrieve` |
| `kb.answer` | `answer.answer(...)` | `kb:answer` |
| `kb.get_document` | `source_lookup.get_source(...)` | `kb:read` |
| `kb.list_artifacts` | `retrieval.list_artifacts(...)` | `kb:read` |
| `kb.get_artifact` | `retrieval.get_artifact(...)` | `kb:read` |
| `kb.get_citation` | `citation_lookup.get_citations(...)` | `kb:read` |

### Write tools (default-DISABLED — must be explicitly enabled)

| MCP tool name (suggested) | Backed by | Required scope |
|---|---|---|
| `kb.upload_document` | `ingestion.register_document(...)` | `kb:ingest` |
| `kb.start_ingestion_job` | `job_control.start_project_job(...)` | `kb:ingest` |
| `kb.submit_feedback` | `feedback.submit_feedback(...)` | `kb:feedback` |
| `kb.apply_review_decision` | `review.apply_decision(...)` | `kb:admin` |
| `kb.create_project` | `project_admin.create_project(...)` | `kb:admin` |

The "default-disabled" pattern: the adapter's constructor takes an
`enable_write_tools: bool = False` parameter. When `False` the write
tools are not registered with the MCP server — agents see only the
read tool surface. Production deployments wanting agent-driven
writes opt in explicitly with their own scope-grant policy.

---

## 4. Tool input/output schema discipline

Reuse the existing Pydantic schemas from
[`j1/adapters/rest/schemas.py`](../src/j1/adapters/rest/schemas.py)
and [`j1/integration/bulk/schemas.py`](../src/j1/integration/bulk/schemas.py).
MCP tools take a JSON-schema input and return a JSON-schema output —
the existing camelCase Pydantic models serialise straight into both.

Do **not** define MCP-specific DTOs that duplicate REST shapes. If a
tool needs a slightly different view, add a method to the appropriate
service in `j1.integration.services` and call that from both the REST
handler and the MCP tool.

---

## 5. Errors

MCP defines its own JSON-RPC error structure. The adapter MUST mask
internal exceptions exactly as the SSE adapter does:

| Source exception | MCP error |
|---|---|
| `AuthenticationError` | `-32001` (unauthorized) — no exception text leaked |
| `AuthorizationError` | `-32003` (forbidden) — `data.required_scope` set |
| `DocumentNotFoundError` / `ArtifactNotFoundError` | `-32602` (invalid params) — public message only |
| Any other `J1Error` | `-32603` (internal error) — generic safe message; full exception logged with `correlation_id` |

The numeric codes above are the JSON-RPC convention; the framework's
own error codes (`UNAUTHENTICATED`, `INSUFFICIENT_SCOPE`, …) live in
`data.code` so consumers can branch consistently with REST.

---

## 6. Event publication

If the MCP adapter is wired with an `event_bus` (mirroring
`create_rest_api(event_bus=...)`), each tool call SHOULD publish the
same `ApplicationEvent` types the REST handlers publish. The
[`publish_*`](../src/j1/adapters/rest/events.py) helpers in
`j1.adapters.rest.events` are intentionally generic — they can be
reused verbatim from an MCP adapter, or replicated via the same
pattern.

This keeps webhook subscribers and broker consumers transparent to
which transport produced the event.

---

## 7. When this doc gets deleted

This file goes away when:

- A `src/j1/adapters/mcp/` package exists.
- It implements the contracts in §§ 2–6.
- It has a corresponding `tests/test_rest_mcp.py` (or similar)
 covering at least: read-tool dispatch, write-tool default-disable,
 scope enforcement, error masking, security-context propagation.
- The MCP-adjacent rows in `external-integration-architecture.md` § 2
 switch from "not shipped" to "shipped".
- A `docs/mcp.md` replaces this file with the user-facing guide.

Until then: **MCP is intentionally absent from the framework**.
