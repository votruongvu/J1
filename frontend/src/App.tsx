/**
 * Top-level app shell.
 *
 * Owns:
 *   - persisted user preferences (tenant / project / auth / theme / mode / scenario)
 *   - the active `IngestionClient` (mock or live), shared via context
 *   - the route state machine (`list` | `upload` | `run`)
 *   - the toast queue
 *
 * Page components consume the client via `useClient()` so neither
 * routing nor data-source selection leaks into them.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient } from "@/lib/api/api-client";
import type { IngestionClient } from "@/lib/api/client";
import { MockClient, MOCK_PROJECT, MOCK_TENANT } from "@/lib/api/mock-client";
import { ClientContext } from "@/lib/client-context";
import { LS_KEYS, useLocalStorage } from "@/lib/hooks/useLocalStorage";
import { ContextBar } from "@/components/ContextBar";
import { AuthModal } from "@/components/AuthModal";
import { LLMHealthBanner } from "@/components/LLMHealthBanner";
import { ToastHost } from "@/components/Toast";
import { AllRunsPage } from "@/pages/AllRunsPage";
import { UploadPage } from "@/pages/UploadPage";
import { RunDetailPage } from "@/pages/RunDetailPage";
import type { AuthConfig, AuthKind, MockScenario, Mode, Route, Theme, Toast } from "@/types/ui";

// Vite inlines `import.meta.env.VITE_API_BASE_URL` at build time. The
// dev compose stack sets this to `/api` so the browser stays
// single-origin (nginx proxies to FastAPI). Falls back to a direct
// `http://localhost:8000` for local-host dev runs (`npm run dev`)
// where Vite serves the SPA on 5173 and the API runs on 8000.
const DEFAULT_API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export function App() {
  const [tenant, setTenant] = useLocalStorage(LS_KEYS.tenant, MOCK_TENANT);
  const [project, setProject] = useLocalStorage(LS_KEYS.project, MOCK_PROJECT);
  const [authKind, setAuthKind] = useLocalStorage<AuthKind>(LS_KEYS.authKind, "bearer");
  const [authValue, setAuthValue] = useLocalStorage(LS_KEYS.authValue, "");
  const [apiBase, setApiBase] = useLocalStorage(LS_KEYS.apiBase, DEFAULT_API_BASE);
  const [theme, setTheme] = useLocalStorage<Theme>(LS_KEYS.theme, "light");
  // Default to LIVE mode. Mock mode hardcodes success responses for
  // many client methods (getLLMHealth, getRunContentInventory,
  // getRunPlanning) which silently masks real backend issues:
  // operators were seeing tabs locked / banners missing because
  // the mock returned `healthy: true` / `unavailable: true` while
  // the live backend was reporting the actual state. Live is the
  // safer default for a first-load on a real deployment; existing
  // users keep their persisted preference via useLocalStorage.
  // Demo / standalone testing can still flip to mock via the toggle.
  const [mode, setMode] = useLocalStorage<Mode>(LS_KEYS.mode, "live");
  const [scenario, setScenario] = useLocalStorage<MockScenario>(LS_KEYS.scenario, "warnings");

  const [authOpen, setAuthOpen] = useState(false);
  const [route, setRoute] = useState<Route>({ name: "list" });
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Apply the chosen theme to <html data-theme="…">.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Build the active client. We keep a stable MockClient instance so
  // its scenario state and run cache survive scenario changes; the
  // ApiClient is rebuilt only when its inputs (baseUrl / context /
  // auth) change.
  const mockClientRef = useMemo(() => new MockClient(), []);

  const client = useMemo<IngestionClient>(() => {
    if (mode === "live") {
      return new ApiClient({
        baseUrl: apiBase,
        getCtx: () => ({ tenant, project }),
        getAuth: () => ({ kind: authKind, value: authValue }),
      });
    }
    return mockClientRef;
  }, [mode, apiBase, tenant, project, authKind, authValue, mockClientRef]);

  // Keep mock scenario in sync with state.
  useEffect(() => {
    mockClientRef.setScenario(scenario);
  }, [mockClientRef, scenario]);

  const ctx = useMemo(() => ({ tenant, project }), [tenant, project]);
  const setCtx = (next: { tenant: string; project: string }) => {
    setTenant(next.tenant);
    setProject(next.project);
  };
  const auth: AuthConfig = useMemo(
    () => ({ kind: authKind, value: authValue }),
    [authKind, authValue],
  );
  const setAuth = ({ kind, value }: AuthConfig) => {
    setAuthKind(kind);
    setAuthValue(value);
  };

  const pushToast = useCallback((t: Omit<Toast, "id">) => {
    const id = Math.random().toString(36).slice(2);
    const toast: Toast = { id, ...t };
    setToasts((prev) => [...prev, toast]);
    setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== id)), 4500);
  }, []);

  const dismissToast = (id: string) => setToasts((prev) => prev.filter((x) => x.id !== id));

  const onUploaded = (runId: string) => setRoute({ name: "run", runId });

  const onLoadDemo = async () => {
    try {
      const { runId } = await client.upload({ name: "earnings-2024-q4.pdf" }, ctx);
      onUploaded(runId);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Upload failed.";
      pushToast({ kind: "error", title: "Upload failed", body: message });
    }
  };

  return (
    <ClientContext.Provider value={client}>
      <div className="app">
        <ContextBar
          ctx={ctx}
          setCtx={setCtx}
          auth={auth}
          onAuthClick={() => setAuthOpen(true)}
          theme={theme}
          onThemeToggle={() => setTheme(theme === "dark" ? "light" : "dark")}
          mode={mode}
          onModeToggle={() => setMode(mode === "live" ? "mock" : "live")}
        />

        <LLMHealthBanner />

        <main className="main">
          {route.name === "list" && (
            <AllRunsPage
              ctx={ctx}
              onOpenRun={(runId) => setRoute({ name: "run", runId })}
              onNewRun={() => setRoute({ name: "upload" })}
              pushToast={pushToast}
            />
          )}
          {route.name === "upload" && (
            <UploadPage
              ctx={ctx}
              onUploaded={onUploaded}
              onLoadDemo={() => void onLoadDemo()}
              scenario={scenario}
              onScenarioChange={setScenario}
              onBack={() => setRoute({ name: "list" })}
            />
          )}
          {route.name === "run" && (
            <RunDetailPage
              runId={route.runId}
              ctx={ctx}
              onBack={() => setRoute({ name: "list" })}
              pushToast={pushToast}
            />
          )}
        </main>

        <AuthModal
          open={authOpen}
          onClose={() => setAuthOpen(false)}
          auth={auth}
          onSave={setAuth}
          apiBase={apiBase}
          onApiBaseChange={setApiBase}
        />

        <ToastHost toasts={toasts} onDismiss={dismissToast} />
      </div>
    </ClientContext.Provider>
  );
}
