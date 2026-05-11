/**
 * Wave 9B — Initial Execution Plan panel.
 *
 * Renders the pre-compile `InitialExecutionPlan` artifact via
 * `GET /ingestion-runs/{id}/initial-execution-plan`. The plan
 * carries the resolved DOMAIN PACK, the resolved ENRICHMENT POLICY
 * (auto / always / never), the require-enrichment-success flag,
 * the CANDIDATE MODULES, and a cheap-signals snapshot the workflow
 * used to choose them.
 *
 * Mirrors the EnrichPlanPanel state machine — loading / unavailable
 * / ready / error — so a panel mounted before pre-compile build
 * picks up the artifact when it lands.
 */

import { useCallback, useEffect, useState } from "react";

import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import type {
  InitialExecutionPlanPayload,
  RunInitialExecutionPlanResponse,
} from "@/types/review";


interface InitialExecutionPlanPanelProps {
  runId: string;
  latestEvent?: ProgressEvent | null;
}


type PanelState =
  | { kind: "loading" }
  | { kind: "unavailable"; reason: string | null }
  | {
      kind: "ready";
      resp: RunInitialExecutionPlanResponse;
      plan: InitialExecutionPlanPayload;
    }
  | { kind: "error"; message: string };


export function InitialExecutionPlanPanel({
  runId,
  latestEvent,
}: InitialExecutionPlanPanelProps) {
  const client = useClient();
  const [state, setState] = useState<PanelState>({ kind: "loading" });

  const loadPlan = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunInitialExecutionPlan(runId);
        if (cancelled) return;
        if (resp.status === "completed" && resp.plan) {
          setState({ kind: "ready", resp, plan: resp.plan });
        } else {
          // Keep the prior ready state when a refetch returns
          // unavailable — the artifact might still be in flux.
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
      EVENT_TYPES.STEP_SKIPPED,
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
      <div className="card" data-testid="initial-plan-loading">
        Loading initial execution plan…
      </div>
    );
  }
  if (state.kind === "unavailable") {
    return (
      <div className="card" data-testid="initial-plan-unavailable">
        <h3>Initial Execution Plan</h3>
        <p style={{ color: "var(--text-muted)" }}>
          {state.reason
            ?? "Initial execution plan is not available for this run yet."}
        </p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="initial-plan-error">
        <h3>Initial Execution Plan</h3>
        <p style={{ color: "var(--error-fg)" }}>
          Couldn't load initial execution plan: {state.message}
        </p>
      </div>
    );
  }
  return <InitialExecutionPlanContent plan={state.plan} />;
}


function InitialExecutionPlanContent({
  plan,
}: {
  plan: InitialExecutionPlanPayload;
}) {
  const candidates = plan.candidate_modules ?? [];
  const reasons = plan.reasons ?? [];
  const warnings = plan.warnings ?? [];
  return (
    <div data-testid="initial-plan-panel">
      <div className="card" data-testid="initial-plan-card">
        <h3>Initial Execution Plan</h3>
        <dl className="kv">
          <dt>Domain profile</dt>
          <dd data-testid="initial-plan-domain">
            <span className="badge">
              {plan.domain_profile_id ?? "general"}
            </span>
          </dd>
          <dt>Domain enrichment policy</dt>
          <dd data-testid="initial-plan-policy">
            <span
              className={`badge enrich-policy enrich-policy--${plan.enrichment_policy ?? "auto"}`}
            >
              {policyLabel(plan.enrichment_policy)}
            </span>
          </dd>
          <dt>Require enrichment success</dt>
          <dd data-testid="initial-plan-require-success">
            {plan.require_enrichment_success
              ? "Yes — a failed enrichment will fail the run"
              : "No — enrichment is best-effort"}
          </dd>
          <dt>Candidate enrichment modules</dt>
          <dd>
            {candidates.length === 0 ? "—" : (
              <div className="cap-pills" data-testid="initial-plan-candidates">
                {candidates.map((m) => (
                  <span key={m} className="badge cap-pill">
                    {humaniseModuleId(m)}
                  </span>
                ))}
              </div>
            )}
          </dd>
          {reasons.length > 0 && (
            <>
              <dt>Reasons</dt>
              <dd>
                <ul className="bullet-list">
                  {reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </dd>
            </>
          )}
          {warnings.length > 0 && (
            <>
              <dt>Warnings</dt>
              <dd>
                <ul
                  className="bullet-list"
                  data-testid="initial-plan-warnings"
                  style={{ color: "var(--warning-fg)" }}
                >
                  {warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}


function policyLabel(policy: string | null | undefined): string {
  if (!policy) return "auto";
  switch (policy) {
    case "auto":
      return "Auto — decide per-run from compile signals";
    case "always":
      return "Always — run enrichment whenever compile succeeds";
    case "never":
      return "Never — domain enrichment is disabled";
    default:
      return policy;
  }
}


function humaniseModuleId(id: string): string {
  // Module ids are snake_case (e.g. `metadata_enrichment`) — turn
  // them into title case for the badge label without doing a heavy
  // i18n lookup.
  return id
    .split("_")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}
