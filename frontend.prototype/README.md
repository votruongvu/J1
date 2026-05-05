# J1 Execution Console — frontend

User-facing UI for J1 ingestion runs. Surfaces the **plan**, **live
progress**, and **final result** of each ingestion — no terminal log
wall, no Temporal UI dependency. Designed in the spirit of progress
views in Claude Code, Copilot, and Codex.

This directory ships the design verbatim from the
[J1 Execution Console design package](https://api.anthropic.com/v1/design/h/djR_sCibAZdQOtwDUFRAag),
plus a real `ApiClient` (`api-client.jsx`) that talks to the J1 REST
backend at `/ingestion-runs/*`.

## Run it

The console is a static SPA — no build step. Open it in a browser:

```bash
# From the repo root
python3 -m http.server 5173 --directory frontend
# → http://localhost:5173
```

(or any other static file server — `npx serve frontend`,
`browser-sync start --server frontend`, etc.)

The first load uses **Mock mode** so the entire flow is exercisable
without a backend. Toggle to **Live API** in the context bar (top
right) to talk to a running J1 worker + REST API.

## Dual-mode (mock ↔ live)

The UI runs against either:

- **Mock mode** (default) — `mock-api.jsx` simulates every endpoint
  with a scripted event timeline. Three demo scenarios on the upload
  screen (warnings / failure / human review). Useful for design,
  demos, and frontend development without a backend.
- **Live API** — `api-client.jsx` issues real HTTP requests against
  the J1 REST surface and consumes the SSE event stream via
  `fetch` + `ReadableStream` (native `EventSource` can't send custom
  headers).

Toggle persists to `localStorage["j1.mode"]`. Both clients share the
same interface; component code never branches on mode.

## Tenant / Project context

The J1 REST contract requires `X-Tenant-Id` and `X-Project-Id` on
every request. The sticky context bar at the top of every page reads
the values and persists them to `localStorage` (`j1.tenantId` /
`j1.projectId`). When either is empty, the upload action is
disabled and the UI surfaces:

> Tenant and Project are required. Please set them in the context bar.

The Live API client injects both headers on every request, including
the SSE stream.

## Auth

The Authorize modal (top-right) supports either:

- **Bearer token** → sent as `Authorization: Bearer <token>`
- **X-API-Key** → sent as `X-API-Key: <key>`

Stored under `j1.authKind` / `j1.authValue` for local development.
Use the **Clear** button to remove. Secrets are never logged.

In Live mode against a J1 backend with `J1_AUTH_API_KEYS` configured,
set the matching token here. With auth disabled, leave blank.

## API base URL

Set in the Authorize modal. Stored under `localStorage["j1.apiBase"]`.
Defaults to `http://localhost:8000` (matches `deploy/dev/docker-compose.yml`).

In production deployments, the deployment serves both the API and
the static frontend; set the base URL to the same origin or a
relative path.

## Architecture

```
index.html                — entry, loads scripts in order
styles.css                — design tokens (light/dark) + components
icons.jsx                 — minimal stroke-icon set
mock-api.jsx              — MockClient + scripted event stream + display mappings
api-client.jsx            — ApiClient — real fetch-based client against /ingestion-runs/*
ui.jsx                    — StatusBadge / DecisionBadge / ProgressBar / Modal / Toast / JsonView
run-detail.jsx            — RunHeader / PlanCard / LiveTimeline / FinalResult / TechDrawer / PrimaryStatusPanel
upload.jsx                — UploadScreen (dropzone, scenario picker, demo button)
all-runs.jsx              — AllRunsPage (list, filters, quick-chips)
app.jsx                   — App shell, ContextBar (mode toggle), AuthModal, RunDetailPage, routing
```

## Live API contract

The `ApiClient` connects to the J1 REST surface I shipped in this
repo. Endpoints used:

| Method | Path                                          | Used for |
|---|---|---|
| `POST` | `/ingestion-runs`                             | Upload + create run |
| `GET`  | `/ingestion-runs/{run_id}`                    | Run status snapshot |
| `GET`  | `/ingestion-runs/{run_id}/plan`               | Execution plan |
| `POST` | `/ingestion-runs/{run_id}/confirm`            | Confirm plan |
| `GET`  | `/ingestion-runs/{run_id}/events`             | Backfill timeline |
| `GET`  | `/ingestion-runs/{run_id}/events/stream`      | Live SSE stream |

**Field translation** lives entirely in `api-client.jsx`. The J1 API
returns camelCase envelopes (`{ requestId, data, meta }` with
`runId` / `documentId` / `progressPercent` / etc.); the design's
components use a partly-snake_case shape (`run.document_name`,
`run.progress_pct`, `run.started_at`). The translation happens in
`_runFromApi` / `_planFromApi` / `_eventFromApi` so component code
stays unchanged.

### List view in live mode

The J1 API does not yet ship a `GET /ingestion-runs` list endpoint.
In live mode the All Runs page shows an empty state — click
**New ingestion run** to create one. The run-detail flow (status,
plan, confirm, events, SSE stream) works end-to-end.

When the backend adds a list endpoint, replace the `listRuns`
implementation in `api-client.jsx` and the All Runs page picks it up
automatically.

## SSE — why `fetch` + `ReadableStream`?

Native `EventSource` cannot send custom request headers. Tenant /
Project headers are mandatory per the J1 contract, so `ApiClient`
implements SSE with:

```
fetch(url, { headers, signal: controller.signal })
  → response.body.getReader()
    → parse "id:<>\nevent:<>\ndata:<json>\n\n" frames
      → handlers.onEvent(structuredEvent)
```

Reconnect: on stream error, the App keeps the latest `eventId` and
calls `openStream` again with `lastEventId` in the `Last-Event-Id`
header. The J1 backend resumes from after that cursor. After
reconnect, the App also re-fetches `GET /events` to fill any gap.
Events are de-duplicated client-side by `eventId`.

## End-to-end smoke test (live mode)

```bash
# 1. Bring the J1 dev stack up.
docker compose -f deploy/dev/docker-compose.yml up -d --build

# 2. Serve the frontend.
python3 -m http.server 5173 --directory frontend

# 3. Open http://localhost:5173.
#    Set Tenant=acme, Project=alpha in the context bar.
#    Click "Mock mode" → switches to "Live API".
#    Click "New ingestion run" → upload a file.
#    The plan appears, run starts, progress events stream in.
```

If you see CORS errors, the J1 dev API doesn't enable CORS by
default. Either:
- Add a CORS middleware to `create_rest_api()` for development, or
- Serve both the frontend and API behind the same origin (recommended
  for production).

## States covered

- empty upload state
- tenant/project missing → context warning + disabled upload
- auth modal (set / clear)
- assessing → plan ready → awaiting confirmation
- running with live progress + step.progress bars
- step.warning (amber) / step.failed (red)
- run.completed, run.completed-with-warnings, run.failed
- human_review.required
- stream reconnecting indicator
- 400 missing context, 401/403 unauthorized, 404 run-not-found, 5xx retryable (toast surface)
- malformed events do not crash; raw payload visible in the Technical Details drawer

## Display mappings (centralized)

All status / decision / severity → label + style mappings live in
`mock-api.jsx`:

- `StatusDisplay` — `CREATED`, `ASSESSING`, `PLAN_READY`,
  `WAITING_FOR_CONFIRMATION`, `RUNNING`, `COMPLETED`,
  `COMPLETED_WITH_WARNINGS`, `SUCCEEDED`, `SUCCEEDED_WITH_WARNINGS`,
  `FAILED`, `AWAITING_HUMAN_REVIEW`, `REQUIRES_HUMAN_REVIEW`,
  `CANCELLED`
- `DecisionDisplay` — `RUN`, `SKIP`, `CONDITIONAL`
- `SeverityDisplay` — `INFO` → neutral, `WARNING` → amber, `ERROR` → red
- `StageDisplay` — `COMPILE`, `ENRICH`, `GRAPH`, `INDEX`

These are the only place to edit when display strings change.

## Replacing the prototype with a Vite + TS app

When you outgrow Babel-standalone (slow first-load, no type
checking, no tree-shaking), port to Vite + React + TypeScript:

```bash
npm create vite@latest j1-console -- --template react-ts
cd j1-console && npm i @microsoft/fetch-event-source
# Then port src/* into the Vite project.
```

The component logic is small (~3,900 lines). Keep `styles.css`
verbatim — it's pure CSS with design tokens. Convert the script-tag
loading pattern (`window.X`) to ES module imports/exports. The
`ApiClient` module is already framework-agnostic and ports as-is.
