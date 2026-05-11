# J1 Execution Console — Frontend

User-facing console for J1 ingestion runs, built with **Vite + React 18 + TypeScript**.

## Scripts

```bash
npm install # install dependencies
npm run dev # local dev server at http://localhost:5173
npm run build # type-check + production build → dist/
npm run preview # serve the production build
npm run typecheck # tsc --noEmit
npm run lint # eslint with --max-warnings 0
npm run lint:fix # eslint --fix
npm run format # prettier --write
npm run format:check # prettier --check
npm run ci # typecheck + lint + format:check + build (run this in CI)
```

## Layout

```
src/
├── components/ Reusable UI primitives (badges, modal, banner, toast, JSON view, context bar, auth modal)
│ └── icons.tsx SVG icon set
├── pages/
│ ├── UploadPage.tsx
│ ├── AllRunsPage.tsx
│ ├── RunDetailPage.tsx (orchestrator)
│ └── run-detail/ header, plan card, live timeline, primary status panel, tech drawer
├── lib/
│ ├── api/ IngestionClient interface, mock client, live (REST + fetch-based SSE) client, translator
│ ├── hooks/ useLocalStorage, useClient
│ ├── client-context.tsx React context for the active client
│ ├── display.ts centralised status / decision / severity / stage / event-type mappings
│ └── format.ts relativeTime helper
├── types/
│ ├── ingestion.ts domain types — runs, plans, events
│ └── ui.ts theme, mode, route, toast, etc.
├── App.tsx
├── main.tsx
└── styles.css
```

## Architecture notes

- **Single integration surface.** The `IngestionClient` interface in `src/lib/api/client.ts` is implemented by both `MockClient` and `ApiClient`; component code never branches on data origin.
- **Translation isolation.** `src/lib/api/translate.ts` is the only file that maps J1's camelCase REST envelopes onto the frontend domain shapes. When the backend contract changes, edit this file and `src/types/ingestion.ts` only.
- **Centralised display strings.** `src/lib/display.ts` holds every label / colour / class name driven by a backend enum. Don't sprinkle status strings across components.
- **No native EventSource.** The live client streams SSE via `fetch` + `ReadableStream` so it can send the required `X-Tenant-Id` / `X-Project-Id` / auth headers (which `EventSource` forbids). The parser lives in `src/lib/api/sse.ts`.
- **Persisted preferences.** Tenant, project, auth, API base URL, theme, mode, and mock scenario are stored in `localStorage` under the keys defined in `src/lib/hooks/useLocalStorage.ts`.
- **Mock vs live mode** is toggled in the context bar. Mock mode runs entirely in the browser with a scripted event timeline; live mode hits the real REST surface.

## Backend endpoints used (live mode)

```
POST /ingestion-runs upload + create run
GET /ingestion-runs/{run_id} status snapshot
GET /ingestion-runs/{run_id}/plan execution plan
POST /ingestion-runs/{run_id}/confirm confirm plan
GET /ingestion-runs/{run_id}/events historical events
GET /ingestion-runs/{run_id}/events/stream live SSE
```

The `GET /ingestion-runs` list endpoint is not yet implemented on the backend. In live mode the All Runs view shows an explanatory banner; create runs from the Upload page and the run-detail screen works end-to-end.

## Docker

The dev compose stack ships the SPA as part of `docker compose up`
(see [`../deploy/dev/README.md`](../deploy/dev/README.md)). The
[`Dockerfile`](Dockerfile) is multi-stage:

1. **`deps`** — `npm ci` against `package*.json` only, so source
 edits don't bust the cache.
2. **`build`** — runs `npm run build` with `VITE_API_BASE_URL`
 wired in as a build arg.
3. **`run`** — `nginx:alpine` serves `dist/` and proxies `/api/*`
 to the `api` service over the Docker bridge network. Browser
 stays single-origin → no CORS allow-list needed on the backend.

Build standalone:

```bash
# Default — bundle hits `/api` (use behind a same-origin proxy)
docker build -t j1-frontend frontend/

# Point the bundle at an absolute backend URL
docker build \
 --build-arg VITE_API_BASE_URL=https://api.example.com \
 -t j1-frontend frontend/
```

Or via compose (rebuilds on source change):

```bash
docker compose -f deploy/dev/docker-compose.yml up --build frontend
```

The host port and API base URL are controlled by `J1_FRONTEND_PORT`
and `J1_FRONTEND_API_BASE_URL` in [`../.env`](../.env).

## Reference snapshot

The original Babel-standalone HTML/JSX prototype is preserved verbatim under [`frontend.prototype/`](../frontend.prototype/) for design reference.
