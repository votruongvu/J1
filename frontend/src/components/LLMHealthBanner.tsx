/**
 * Top-of-page banner that surfaces LLM connectivity status.
 *
 * Polls `/healthz/llm` (cached on the backend; no upstream LLM call
 * per request) every 30s. Renders nothing when healthy. Renders a
 * dismissible warning row listing each unreachable role + its error
 * when not healthy.
 *
 * The banner is informational; the actual upload-disable lives on
 * `UploadPage` via the same `useLLMHealth` hook so the buttons stay
 * in sync with the banner without prop-drilling.
 */

import { useEffect, useState } from "react";
import { useClient } from "@/lib/client-context";
import type { LLMHealthStatus } from "@/lib/api/client";

const POLL_INTERVAL_MS = 30_000;

/** Hook returning `[status, refresh]`:
 *
 *   - `status`: the latest cached health snapshot (null until the
 *     first fetch completes). Polled on a 30s interval.
 *   - `refresh`: callable that POSTs `/healthz/llm/refresh` so the
 *     backend re-probes synchronously and updates the cache. Used
 *     by the banner's "Retry now" button so admins don't have to
 *     wait up to 30s after fixing the LLM endpoint to verify.
 */
export function useLLMHealth(): [LLMHealthStatus | null, () => Promise<void>] {
  const client = useClient();
  const [status, setStatus] = useState<LLMHealthStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const result = await client.getLLMHealth();
        if (!cancelled) setStatus(result);
      } catch {
        // Health endpoint unreachable → treat as unhealthy so the
        // banner appears. Operators usually want SOMETHING on screen
        // when /healthz/llm itself fails.
        if (!cancelled) {
          setStatus({
            healthy: false,
            checkedAt: new Date().toISOString(),
            results: [{
              role: "api",
              ok: false,
              provider: null,
              model: null,
              error: "API health endpoint unreachable",
            }],
          });
        }
      }
    };
    void tick();
    const id = setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [client]);

  const refresh = async () => {
    try {
      const result = await client.refreshLLMHealth();
      setStatus(result);
    } catch {
      setStatus({
        healthy: false,
        checkedAt: new Date().toISOString(),
        results: [{
          role: "api",
          ok: false,
          provider: null,
          model: null,
          error: "API refresh endpoint unreachable",
        }],
      });
    }
  };

  return [status, refresh];
}

export function LLMHealthBanner() {
  const [status, refresh] = useLLMHealth();
  const [retrying, setRetrying] = useState(false);

  // Quiet when first-loading or healthy. We don't render a "checking"
  // state — operators want signal, not noise.
  if (status === null || status.healthy) return null;

  const failures = status.results.filter((r) => !r.ok);
  const checkedAt = status.checkedAt
    ? new Date(status.checkedAt).toLocaleString()
    : "unknown";

  const onRetry = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      await refresh();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div
      role="status"
      style={{
        background: "var(--surface-warning, #fff7e6)",
        borderBottom: "1px solid var(--border-warning, #f0ad4e)",
        padding: "10px 16px",
        fontSize: 13,
      }}
    >
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
      }}>
        <div>
          <strong style={{ color: "var(--text-warning, #b76d00)" }}>
            LLM unreachable — admin notice
          </strong>
          <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>
            New ingestion runs may fail until the endpoint(s) below recover.
          </span>
        </div>
        <button
          type="button"
          onClick={() => void onRetry()}
          disabled={retrying}
          style={{
            background: "transparent",
            border: "1px solid var(--border-warning, #f0ad4e)",
            color: "var(--text-warning, #b76d00)",
            padding: "4px 10px",
            fontSize: 12,
            borderRadius: 4,
            cursor: retrying ? "wait" : "pointer",
            whiteSpace: "nowrap",
          }}
          title="Re-probe the LLM endpoint synchronously"
        >
          {retrying ? "Checking…" : "Retry now"}
        </button>
      </div>
      <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>
        {failures.map((f) => (
          <li key={f.role} style={{ marginBottom: 2 }}>
            <code style={{ fontWeight: 600 }}>{f.role}</code>
            {f.provider ? ` · ${f.provider}` : ""}
            {f.model ? ` / ${f.model}` : ""}
            {f.error ? (
              <span style={{ color: "var(--text-muted)", marginLeft: 6 }}>
                — {f.error}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      <div style={{ marginTop: 6, color: "var(--text-muted)", fontSize: 12 }}>
        Last checked: {checkedAt}. The background monitor re-probes
        every 30s; click <em>Retry now</em> to verify a fix immediately.
      </div>
    </div>
  );
}
