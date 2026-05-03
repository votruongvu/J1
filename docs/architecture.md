# J1 Architecture

## Layer overview

```
┌─────────────────────────────────────────────────────────┐
│  Protocol adapters    j1.adapters.rest (REST/FastAPI)   │
│                       (future) MCP / Webhook / AsyncAPI │
└──────────────────────────┬──────────────────────────────┘
                           │ depends on
                           ▼
┌─────────────────────────────────────────────────────────┐
│  Integration boundary  j1.integration                   │
│   - ports (Protocol interfaces)                         │
│   - DTOs (protocol-neutral dataclasses)                 │
│   - default port impls                                  │
│   - ApplicationFacade                                   │
└──────────────────────────┬──────────────────────────────┘
                           │ depends on
                           ▼
┌─────────────────────────────────────────────────────────┐
│  Application services                                   │
│   - DocumentIntakeService, ProcessingService,           │
│     HybridQueryEngine, SqliteSearchIndexer, etc.        │
└──────────────────────────┬──────────────────────────────┘
                           │ depends on
                           ▼
┌─────────────────────────────────────────────────────────┐
│  Core / domain                                          │
│   - intake, processing, query, search, artifacts,       │
│     audit, cost, review, connectors, enrichers,         │
│     orchestration (Temporal substrate is internal)      │
└─────────────────────────────────────────────────────────┘
```

The dependency arrow points one way only — **outward layers depend on inward layers, never the reverse**.

## What the External Integration Layer is

`j1.integration` is the boundary at which external systems integrate with the J1 knowledge base. It contains three things and only three things:

1. **Ports** (`j1/integration/ports.py`) — `Protocol` classes that describe what J1 exposes to the outside world. One per capability:
   - `DocumentIngestionPort`, `JobStatusPort`, `JobControlPort`, `SearchPort`,
     `RetrievalPort`, `AnswerPort`, `CitationLookupPort`, `SourceLookupPort`,
     `FeedbackPort`, `EventPublisherPort`, `ProjectAdminPort`,
     `CostSummaryPort`, `ReviewPort`
2. **DTOs** (`j1/integration/dto.py`) — protocol-neutral frozen dataclasses for request/response payloads. No HTTP-isms, no Temporal types, no Pydantic. Adapters map their wire format to/from these.
3. **Default port implementations** (`j1/integration/services.py`) — thin classes that wire the ports to existing J1 services. Bundled in `ApplicationFacade`.

Plus one piece of new infrastructure that didn't exist before:

4. **Feedback storage** (`j1/integration/feedback.py`) — `FeedbackRecord` + `JsonlFeedbackStore` writing to `<project>/runtime/feedback.jsonl`. Mirrors the audit/cost sink pattern.

## What belongs in the External Integration Layer

- Port `Protocol` definitions
- DTOs (request/response)
- Default port implementations that delegate to J1 services
- `ApplicationFacade` — the bundle adapters consume

## What must NOT be placed there

- HTTP routing, middleware, or status codes (those belong to `j1.adapters.rest`)
- MCP / Webhook / gRPC / AsyncAPI specifics (those belong to future `j1.adapters.<protocol>` modules)
- Pydantic models tied to a specific HTTP framework (DTOs are plain dataclasses)
- Domain logic (lives in core / app-services layer)
- Direct filesystem or network I/O beyond what existing services already do

## Rules enforced by tests

`tests/test_integration_layer.py` includes a static dependency-direction guard that AST-walks every core module and asserts no `import j1.integration` or `import j1.adapters`. The guard fails the build if anyone reverses the dependency arrow.

The same test file verifies `j1.integration` itself never imports from `j1.adapters.*` — adapters depend on integration, not the reverse.

## How to add a future protocol adapter

For a new protocol (e.g. MCP, Webhook, AsyncAPI), create a sibling package under `j1.adapters`:

```
src/j1/adapters/mcp/
├── __init__.py
├── server.py           # MCP-specific server / handler classes
└── mappers.py          # protocol-format ↔ DTO mapping
```

The adapter:
1. Receives protocol-specific input (an MCP message, a webhook POST, an AsyncAPI envelope)
2. Maps it to a DTO (`AnswerRequestDTO`, `FeedbackDTO`, `ProjectIngestionRequestDTO`, etc.)
3. Calls the appropriate port on `ApplicationFacade`
4. Maps the returned DTO back to the protocol's wire format

The adapter never imports from `j1.intake`, `j1.processing`, `j1.query`, etc. directly — only from `j1.integration`.

The existing [`j1.adapters.rest`](../src/j1/adapters/rest/) (FastAPI/REST) is the canonical example — see [docs/rest-api.md](rest-api.md) for its endpoint surface.

## Why the dependency direction stays clean

- Core modules (`j1.intake`, `j1.processing`, etc.) never reference `j1.integration` or `j1.adapters`. The integration layer wraps them; they don't know it exists. This is enforced by `test_core_modules_do_not_import_external_layer`.
- `j1.integration` imports from core to wire ports, but never from `j1.adapters`. Enforced by `test_integration_does_not_import_protocol_adapters`.
- `j1.adapters.*` packages import from core and from `j1.integration`. Free to do so — they're the outermost layer.

This means:
- A new protocol adapter can be added without touching core or integration code.
- A core service can be refactored without breaking adapters (as long as the port contract holds).
- Tests for core run without any HTTP / MCP / Webhook stack imported.
