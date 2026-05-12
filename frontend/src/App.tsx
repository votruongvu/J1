/**
 * Top-level app shell.
 *
 * Owns:
 * - persisted user preferences (tenant / project / auth / theme)
 * - the active `IngestionClient`, shared via context
 * - the route state machine (`list` | `upload` | `run`)
 * - the toast queue
 *
 * Page components consume the client via `useClient` so routing
 * doesn't leak into them.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient } from "@/lib/api/api-client";
import type { IngestionClient } from "@/lib/api/client";
import { ClientContext } from "@/lib/client-context";
import { LS_KEYS, useLocalStorage } from "@/lib/hooks/useLocalStorage";
import { ContextBar } from "@/components/ContextBar";
import { AuthModal } from "@/components/AuthModal";
import { LLMHealthBanner } from "@/components/LLMHealthBanner";
import { ToastHost } from "@/components/Toast";
import { AllRunsPage } from "@/pages/AllRunsPage";
import { DocumentsPage } from "@/pages/DocumentsPage";
import { DocumentDetailPage } from "@/pages/DocumentDetailPage";
import { UploadPage } from "@/pages/UploadPage";
import { RunDetailPage } from "@/pages/RunDetailPage";
import type { AuthConfig, AuthKind, Route, Theme, Toast } from "@/types/ui";

// Vite inlines `import.meta.env.VITE_API_BASE_URL` at build time. The
// dev compose stack sets this to `/api` so the browser stays
// single-origin (nginx proxies to FastAPI). Falls back to a direct
// `http://localhost:8000` for local-host dev runs (`npm run dev`)
// where Vite serves the SPA on 5173 and the API runs on 8000.
const DEFAULT_API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

// Default tenant + project shown on first load. Empty so operators
// see the ContextBar prompt to set them, instead of silently
// inheriting a placeholder that doesn't exist on their backend.
const DEFAULT_TENANT = "";
const DEFAULT_PROJECT = "";

export function App() {
  const [tenant, setTenant] = useLocalStorage(LS_KEYS.tenant, DEFAULT_TENANT);
  const [project, setProject] = useLocalStorage(LS_KEYS.project, DEFAULT_PROJECT);
  const [authKind, setAuthKind] = useLocalStorage<AuthKind>(LS_KEYS.authKind, "bearer");
  const [authValue, setAuthValue] = useLocalStorage(LS_KEYS.authValue, "");
  const [apiBase, setApiBase] = useLocalStorage(LS_KEYS.apiBase, DEFAULT_API_BASE);
  const [theme, setTheme] = useLocalStorage<Theme>(LS_KEYS.theme, "light");

  const [authOpen, setAuthOpen] = useState(false);
  // Default to the document-centric list as part of Phase 7. The
  // legacy run list stays accessible via the nav switcher for the
  // duration of the migration so operators can compare the two
  // surfaces side-by-side.
  const [route, setRoute] = useState<Route>({ name: "documents" });
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Apply the chosen theme to <html data-theme="…">.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Build the live client. Rebuilt only when its inputs (baseUrl /
  // context / auth) change.
  const client = useMemo<IngestionClient>(() => {
    return new ApiClient({
      baseUrl: apiBase,
      getCtx: () => ({ tenant, project }),
      getAuth: () => ({ kind: authKind, value: authValue }),
    });
  }, [apiBase, tenant, project, authKind, authValue]);

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
        />

        <LLMHealthBanner />

        {/* Top-level switcher between the document-centric surface
            and the legacy run-list. Hidden on the run-detail page
            since `← Back` already takes the operator back to the
            list they came from. */}
        {(route.name === "documents" || route.name === "list") && (
          <nav className="main-nav" aria-label="Main view">
            <button
              type="button"
              className={
                "main-nav__tab" +
                (route.name === "documents" ? " main-nav__tab--active" : "")
              }
              onClick={() => setRoute({ name: "documents" })}
              data-testid="nav-documents"
            >
              Documents
            </button>
            <button
              type="button"
              className={
                "main-nav__tab" +
                (route.name === "list" ? " main-nav__tab--active" : "")
              }
              onClick={() => setRoute({ name: "list" })}
              data-testid="nav-runs"
            >
              Runs
              <span className="main-nav__legacy">legacy</span>
            </button>
          </nav>
        )}

        <main className="main">
          {route.name === "documents" && (
            <DocumentsPage
              ctx={ctx}
              onOpenDocument={(documentId) =>
                setRoute({ name: "document", documentId })
              }
              onNewDocument={() => setRoute({ name: "upload" })}
              pushToast={pushToast}
            />
          )}
          {route.name === "document" && (
            <DocumentDetailPage
              documentId={route.documentId}
              ctx={ctx}
              onBack={() => setRoute({ name: "documents" })}
              onOpenRun={(runId) => setRoute({ name: "run", runId })}
              pushToast={pushToast}
            />
          )}
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
              onBack={() => setRoute({ name: "documents" })}
            />
          )}
          {route.name === "run" && (
            <RunDetailPage
              runId={route.runId}
              ctx={ctx}
              onBack={() => setRoute({ name: "documents" })}
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
