// App shell: context bar, auth modal, routing, run-detail orchestration.

const { useState: useStateApp, useEffect: useEffectApp, useRef: useRefApp, useCallback: useCBApp, useMemo: useMemoApp } = React;

const LS_KEYS = {
  tenant: "j1.tenantId",
  project: "j1.projectId",
  auth_kind: "j1.authKind",
  auth_value: "j1.authValue",
  apiBase: "j1.apiBase",
  theme: "j1.theme",
  mode: "j1.mode",  // "mock" | "live"
};

function useLocalStorage(key, initial) {
  const [v, setV] = useStateApp(() => {
    try {
      const x = localStorage.getItem(key);
      return x == null ? initial : x;
    } catch { return initial; }
  });
  const set = useCBApp((nv) => {
    setV(nv);
    try { if (nv == null || nv === "") localStorage.removeItem(key); else localStorage.setItem(key, nv); } catch {}
  }, [key]);
  return [v, set];
}

// ── Context bar ────────────────────────────────────────────────────
function ContextBar({ ctx, setCtx, auth, onAuthClick, theme, onThemeToggle, mode, onModeToggle }) {
  const ok = !!ctx.tenant && !!ctx.project;
  const authed = !!auth.value;
  return (
    <div className="context-bar">
      <div className="context-bar__inner">
        <div className="brand">
          <div className="brand__mark">J1</div>
          <span className="brand__name">Execution Console</span>
          <span className="brand__tag">Ingestion</span>
        </div>

        <div className="ctx-fields">
          <div className="ctx-field">
            <label>Tenant</label>
            <input
              value={ctx.tenant}
              onChange={(e) => setCtx({ ...ctx, tenant: e.target.value })}
              placeholder="tenant-id"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <div className="ctx-field">
            <label>Project</label>
            <input
              value={ctx.project}
              onChange={(e) => setCtx({ ...ctx, project: e.target.value })}
              placeholder="project-id"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <div className={`ctx-status ${ok ? "ctx-status--ok" : "ctx-status--warn"}`}>
            <span className="dot" />
            {ok ? "Context set" : "Context required"}
          </div>
        </div>

        <button
          className="btn btn--sm"
          onClick={onModeToggle}
          aria-label="Toggle data source"
          title={mode === "live" ? "Switch to mock data" : "Switch to live API"}
          style={{
            background: mode === "live" ? "var(--success-soft, #d1fae5)" : "var(--warning-soft, #fef3c7)",
            color: mode === "live" ? "var(--success-fg, #065f46)" : "var(--warning-fg, #92400e)",
          }}
        >
          {mode === "live" ? <Icon.Cpu className="icon-sm" /> : <Icon.Spark className="icon-sm" />}
          {mode === "live" ? "Live API" : "Mock mode"}
        </button>

        <button className="btn btn--sm" onClick={onAuthClick}>
          {authed ? <Icon.Lock className="icon-sm" /> : <Icon.Unlock className="icon-sm" />}
          {authed ? `${auth.kind === "bearer" ? "Bearer" : "API key"} set` : "Authorize"}
        </button>

        <button
          className="theme-toggle"
          onClick={onThemeToggle}
          aria-label="Toggle theme"
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
        >
          {theme === "dark" ? <Icon.Sun className="icon-sm" /> : <Icon.Moon className="icon-sm" />}
        </button>
      </div>
    </div>
  );
}

// ── Auth modal ─────────────────────────────────────────────────────
function AuthModal({ open, onClose, auth, onSave, apiBase, onApiBaseChange }) {
  const [kind, setKind] = useStateApp(auth.kind || "bearer");
  const [value, setValue] = useStateApp(auth.value || "");
  const [reveal, setReveal] = useStateApp(false);
  useEffectApp(() => {
    if (open) { setKind(auth.kind || "bearer"); setValue(auth.value || ""); setReveal(false); }
  }, [open]);

  return (
    <UI.Modal
      open={open}
      onClose={onClose}
      title="Authorize"
      footer={
        <>
          <button className="btn btn--ghost" onClick={() => { onSave({ kind: "bearer", value: "" }); onClose(); }}>
            Clear
          </button>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={() => { onSave({ kind, value: value.trim() }); onClose(); }}>
            <Icon.Check className="icon-sm" /> Save
          </button>
        </>
      }
    >
      <div className="field-group">
        <div className="field">
          <label>API base URL</label>
          <input
            className="input"
            value={apiBase}
            onChange={(e) => onApiBaseChange(e.target.value)}
            placeholder="https://api.j1.example.com"
          />
          <span className="help">Used for all requests; mock mode runs entirely in the browser.</span>
        </div>

        <div className="field">
          <label>Authentication scheme</label>
          <div className="tabs">
            <button className={`tab ${kind === "bearer" ? "is-active" : ""}`} onClick={() => setKind("bearer")}>Bearer token</button>
            <button className={`tab ${kind === "apiKey" ? "is-active" : ""}`} onClick={() => setKind("apiKey")}>X-API-Key</button>
          </div>
        </div>

        <div className="field">
          <label>{kind === "bearer" ? "Bearer token" : "API key"}</label>
          <div style={{ position: "relative" }}>
            <input
              className="input"
              type={reveal ? "text" : "password"}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={kind === "bearer" ? "eyJhbGciOi…" : "sk-…"}
              style={{ width: "100%", paddingRight: 38, fontFamily: "var(--font-mono)" }}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => setReveal(r => !r)}
              style={{ position: "absolute", right: 4, top: 2, height: 28, padding: "0 8px" }}
              aria-label={reveal ? "Hide" : "Show"}
            >
              {reveal ? <Icon.EyeOff className="icon-sm" /> : <Icon.Eye className="icon-sm" />}
            </button>
          </div>
          <span className="help">Stored in localStorage for local development. Cleared by the Clear button.</span>
        </div>
      </div>
    </UI.Modal>
  );
}

// ── Run detail orchestrator ────────────────────────────────────────
function RunDetailPage({ runId, ctx, onBack, pushToast }) {
  const [run, setRun] = useStateApp(null);
  const [plan, setPlan] = useStateApp(null);
  const [events, setEvents] = useStateApp([]);
  const [streamStatus, setStreamStatus] = useStateApp("idle");
  const [confirming, setConfirming] = useStateApp(false);
  const [drawerOpen, setDrawerOpen] = useStateApp(false);
  const [selectedEvent, setSelectedEvent] = useStateApp(null);
  const [runtimeStepStatus, setRuntimeStepStatus] = useStateApp({});
  const streamHandle = useRefApp(null);
  const eventIdsRef = useRefApp(new Set());

  // Initial load
  useEffectApp(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await window.client.getRun(runId);
        if (cancelled) return;
        setRun(r);
        // simulate the assessment → plan timing
        setTimeout(async () => {
          const p = await window.client.getPlan(runId);
          if (cancelled) return;
          setPlan(p);
        }, 600);
        // backfill history (empty initially in mock)
        const hist = await window.client.getEvents(runId);
        if (cancelled) return;
        for (const e of hist) eventIdsRef.current.add(e.eventId);
        setEvents(hist);
        // Open stream right away — it'll deliver assessment + plan events
        openStream();
      } catch (e) {
        if (e.status === 404) {
          pushToast({ kind: "error", title: "Run not found" });
        } else {
          pushToast({ kind: "error", title: "Failed to load run", body: e.message });
        }
      }
    })();
    return () => {
      cancelled = true;
      streamHandle.current?.close?.();
    };
    // eslint-disable-next-line
  }, [runId]);

  const handleEvent = useCBApp((e) => {
    if (eventIdsRef.current.has(e.eventId)) return;
    eventIdsRef.current.add(e.eventId);
    setEvents(prev => [...prev, e]);

    // refresh run snapshot
    window.client.getRun(runId).then(r => setRun(r)).catch(() => {});

    // Track runtime step status for plan card highlighting
    const t = e.event;
    if (t === "step.started") setRuntimeStepStatus(s => ({ ...s, [e.data.step]: "running" }));
    if (t === "step.completed") setRuntimeStepStatus(s => ({ ...s, [e.data.step]: "completed" }));
    if (t === "step.failed") setRuntimeStepStatus(s => ({ ...s, [e.data.step]: "failed" }));
    if (t === "step.skipped") setRuntimeStepStatus(s => ({ ...s, [e.data.step]: "skipped" }));

    if (t === "plan.generated") {
      window.client.getPlan(runId).then(p => setPlan(p)).catch(() => {});
    }
  }, [runId]);

  const openStream = useCBApp(() => {
    if (streamHandle.current) streamHandle.current.close();
    setStreamStatus("open");
    const lastId = events.length > 0 ? events[events.length - 1].eventId : null;
    streamHandle.current = window.client.openStream(runId, {
      lastEventId: lastId,
      onOpen: () => setStreamStatus("open"),
      onEvent: handleEvent,
      onClose: () => setStreamStatus("closed"),
      onError: () => {
        setStreamStatus("reconnecting");
        setTimeout(openStream, 1500);
      },
    });
  }, [runId, events, handleEvent]);

  const onConfirm = async () => {
    setConfirming(true);
    try {
      await window.client.confirm(runId);
      const r = await window.client.getRun(runId);
      setRun(r);
      pushToast({ kind: "success", title: "Plan confirmed", body: "Execution started." });
    } catch (e) {
      pushToast({ kind: "error", title: "Confirm failed", body: e.message });
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <RunHeader run={run} plan={plan} ctx={ctx} onBack={onBack} onOpenDrawer={() => setDrawerOpen(true)} />

      <div style={{ marginBottom: 20 }}>
        <PrimaryStatusPanel run={run} plan={plan} events={events} />
      </div>

      <div className="run-body">
        <div className="col">
          <PlanCard
            plan={plan}
            run={run}
            runtimeStepStatus={runtimeStepStatus}
            onConfirm={onConfirm}
            confirming={confirming}
          />
        </div>
        <div className="col">
          <LiveTimeline
            events={events}
            streamStatus={streamStatus}
            onSelectEvent={(e) => { setSelectedEvent(e); setDrawerOpen(true); }}
          />
        </div>
      </div>

      <TechDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        run={run}
        plan={plan}
        events={events}
        selectedEvent={selectedEvent}
      />
    </div>
  );
}

// ── Top-level App ─────────────────────────────────────────────────
function App() {
  const [tenant, setTenant] = useLocalStorage(LS_KEYS.tenant, MOCK_TENANT);
  const [project, setProject] = useLocalStorage(LS_KEYS.project, MOCK_PROJECT);
  const [authKind, setAuthKind] = useLocalStorage(LS_KEYS.auth_kind, "bearer");
  const [authValue, setAuthValue] = useLocalStorage(LS_KEYS.auth_value, "");
  const [apiBase, setApiBase] = useLocalStorage(LS_KEYS.apiBase, "http://localhost:8000");
  const [theme, setTheme] = useLocalStorage(LS_KEYS.theme, "light");
  const [mode, setMode] = useLocalStorage(LS_KEYS.mode, "mock");

  const [authOpen, setAuthOpen] = useStateApp(false);
  const [route, setRoute] = useStateApp({ name: "list" }); // {name:'list'} | {name:'upload'} | {name:'run', runId}
  const [toasts, setToasts] = useStateApp([]);
  const [scenario, setScenario] = useStateApp("warnings");

  // Apply theme
  useEffectApp(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Configure the active client. Mock and live each have the same
  // surface (`upload / getRun / getPlan / confirm / getEvents /
  // openStream / listRuns`) so the UI never branches on mode.
  useEffectApp(() => {
    if (mode === "live") {
      window.client = new ApiClient({
        baseUrl: apiBase,
        getCtx: () => ({ tenant, project }),
        getAuth: () => ({ kind: authKind, value: authValue }),
      });
    } else {
      if (!(window.client instanceof MockClient)) {
        window.client = new MockClient();
      }
      window.client.setScenario(scenario);
    }
  }, [mode, apiBase, tenant, project, authKind, authValue, scenario]);

  const ctx = { tenant, project };
  const setCtx = (next) => { setTenant(next.tenant); setProject(next.project); };
  const auth = { kind: authKind, value: authValue };
  const setAuth = ({ kind, value }) => { setAuthKind(kind); setAuthValue(value); };

  const pushToast = useCBApp((t) => {
    const id = Math.random().toString(36).slice(2);
    setToasts(prev => [...prev, { id, ...t }]);
    setTimeout(() => setToasts(prev => prev.filter(x => x.id !== id)), 4500);
  }, []);

  const dismissToast = (id) => setToasts(prev => prev.filter(x => x.id !== id));

  const onUploaded = (runId) => setRoute({ name: "run", runId });
  const onLoadDemo = async () => {
    try {
      const { runId } = await window.client.upload({ name: "earnings-2024-q4.pdf" }, ctx);
      onUploaded(runId);
    } catch (e) {
      pushToast({ kind: "error", title: "Upload failed", body: e.message });
    }
  };

  return (
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
          <UploadScreen
            ctx={ctx}
            onUploaded={onUploaded}
            onLoadDemo={onLoadDemo}
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

      <UI.ToastHost toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

// Mount
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<App />);
