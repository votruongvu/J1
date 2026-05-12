/**
 * Compile Result Summary panel.
 *
 * Renders the typed `NormalizedCompileResult` artifact via
 * `GET /ingestion-runs/{id}/compile-result`. Surfaces the post-
 * compile evidence the workflow uses to decide whether enrichment
 * runs: chunks count, extracted text, detected tables/images,
 * retry attempts, and the final quality verdict.
 *
 * Business-friendly copy — uses "Base compile" + "Raw compile
 * output preserved" rather than internal terms. Quality verdict
 * carries the per-attempt history so operators can see when an
 * escalation happened.
 */

import { useCallback, useEffect, useState } from "react";

import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import type {
  NormalizedCompileResultPayload,
  RunCompileResultResponse,
} from "@/types/review";


interface CompileResultPanelProps {
  runId: string;
  latestEvent?: ProgressEvent | null;
}


type PanelState =
  | { kind: "loading" }
  | { kind: "unavailable"; reason: string | null }
  | {
      kind: "ready";
      resp: RunCompileResultResponse;
      plan: NormalizedCompileResultPayload;
    }
  | { kind: "error"; message: string };


export function CompileResultPanel({
  runId,
  latestEvent,
}: CompileResultPanelProps) {
  const client = useClient();
  const [state, setState] = useState<PanelState>({ kind: "loading" });

  const loadPlan = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunCompileResult(runId);
        if (cancelled) return;
        if (resp.status === "completed" && resp.plan) {
          setState({ kind: "ready", resp, plan: resp.plan });
        } else {
          setState((prev) =>
            prev.kind === "ready"
              ? prev
              : {
                  kind: "unavailable",
                  reason: resp.unavailableReason ?? null,
                },
          );
        }
      } catch (e) {
        if (!cancelled) {
          setState((prev) =>
            prev.kind === "ready"
              ? prev
              : {
                  kind: "error",
                  message: e instanceof Error ? e.message : "load failed",
                },
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  useEffect(() => {
    setState({ kind: "loading" });
    return loadPlan();
  }, [loadPlan]);

  useEffect(() => {
    if (!latestEvent) return;
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_COMPLETED,
      EVENT_TYPES.STEP_FAILED,
    ]);
    if (
      refreshOn.has(latestEvent.event)
      || isTerminalEvent(latestEvent.event)
    ) {
      loadPlan();
    }
  }, [latestEvent, loadPlan]);

  if (state.kind === "loading") {
    return (
      <div className="card" data-testid="compile-result-loading">
        <div className="card__body">Loading compile result…</div>
      </div>
    );
  }
  if (state.kind === "unavailable") {
    return (
      <div className="card" data-testid="compile-result-unavailable">
        <div className="card__body">
          <h3>Base Compile Result</h3>
          <p style={{ color: "var(--text-muted)" }}>
            {state.reason
              ?? "Base compile result is not available for this run yet."}
          </p>
        </div>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="compile-result-error">
        <div className="card__body">
          <h3>Base Compile Result</h3>
          <p style={{ color: "var(--error-fg)" }}>
            Couldn't load compile result: {state.message}
          </p>
        </div>
      </div>
    );
  }
  return <CompileResultContent plan={state.plan} />;
}


function CompileResultContent({
  plan,
}: {
  plan: NormalizedCompileResultPayload;
}) {
  const retries = plan.retry_attempts ?? [];
  const retryCount = Math.max(0, retries.length - 1);
  const warnings = plan.warnings ?? [];
  const errors = plan.errors ?? [];
  const rawRefs = plan.raw_artifact_refs ?? [];
  return (
    <div data-testid="compile-result-panel">
      <div className="card" data-testid="compile-result-card">
        <div className="card__body">
          <h3>Base Compile Result</h3>
        <p
          style={{ color: "var(--text-muted)", marginTop: "-0.4rem" }}
          data-testid="compile-result-tagline"
        >
          Output produced by the base compile engine. Raw compile
          output is preserved on disk and accessible via the artifacts
          tab.
        </p>
        <dl className="kv">
          <dt>Parser</dt>
          <dd>
            {plan.parser ?? "—"}
            {plan.parse_method ? ` · ${plan.parse_method}` : ""}
          </dd>
          <dt>Chunks produced</dt>
          <dd data-testid="compile-result-chunks">
            {plan.chunks_count ?? 0}
          </dd>
          <dt>Extracted text</dt>
          <dd data-testid="compile-result-chars">
            {plan.extracted_text_chars
              ? `${plan.extracted_text_chars.toLocaleString()} chars`
              : "—"}
          </dd>
          {plan.page_count != null && (
            <>
              <dt>Pages</dt>
              <dd>{plan.page_count}</dd>
            </>
          )}
          <dt>Detected tables</dt>
          <dd>{(plan.detected_tables ?? []).length}</dd>
          <dt>Detected images</dt>
          <dd>{(plan.detected_images ?? []).length}</dd>
          <dt>Compile retries</dt>
          <dd data-testid="compile-result-retry-count">
            {retryCount === 0
              ? "0 — succeeded on first attempt"
              : `${retryCount} retr${retryCount === 1 ? "y" : "ies"} (${retries.length} attempts)`}
          </dd>
          {plan.final_quality && (
            <>
              <dt>Final quality verdict</dt>
              <dd>
                <span
                  className={`badge quality-verdict quality-verdict--${plan.final_quality}`}
                  data-testid="compile-result-quality"
                >
                  {plan.final_quality}
                </span>
              </dd>
            </>
          )}
          {warnings.length > 0 && (
            <>
              <dt>Warnings</dt>
              <dd>
                <ul
                  className="bullet-list"
                  data-testid="compile-result-warnings"
                  style={{ color: "var(--warning-fg)" }}
                >
                  {warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </dd>
            </>
          )}
          {errors.length > 0 && (
            <>
              <dt>Errors</dt>
              <dd>
                <ul
                  className="bullet-list"
                  data-testid="compile-result-errors"
                  style={{ color: "var(--error-fg)" }}
                >
                  {errors.map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              </dd>
            </>
          )}
          {rawRefs.length > 0 && (
            <>
              <dt>Raw artifact refs</dt>
              <dd
                data-testid="compile-result-raw-refs"
                style={{ fontFamily: "var(--mono)", fontSize: "0.85rem" }}
              >
                {rawRefs.join(", ")}
              </dd>
            </>
          )}
        </dl>
        </div>
      </div>
    </div>
  );
}
