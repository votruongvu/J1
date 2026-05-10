/**
 * Enrich Plan panel — renders the post-compile rule-based enrich
 * assessment for a run. Reads the JSON envelope served by
 * `GET /ingestion-runs/{id}/enrich-plan` (which projects the
 * `post_compile_enrich_plan` artifact). Mirrors
 * `CompileStrategyPanel.tsx`'s shape: lazy fetch, four-state
 * machine (loading / unavailable / ready / error), one banner row
 * + one card.
 */

import { useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  PostCompileEnrichPlanPayload,
  RunEnrichPlanResponse,
} from "@/types/review";
import {
  bannersForEnrichPlan,
  decisionSourceLabel,
  isEnrichPlanAvailable,
  recommendationLabel,
  taskLabel,
} from "./enrich-plan-helpers";

interface EnrichPlanPanelProps {
  runId: string;
}

export function EnrichPlanPanel({ runId }: EnrichPlanPanelProps) {
  const client = useClient();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "unavailable"; reason: string | null }
    | { kind: "ready"; resp: RunEnrichPlanResponse; plan: PostCompileEnrichPlanPayload }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunEnrichPlan(runId);
        if (cancelled) return;
        if (isEnrichPlanAvailable(resp)) {
          setState({ kind: "ready", resp, plan: resp.plan });
        } else {
          setState({
            kind: "unavailable",
            reason: resp.unavailableReason ?? null,
          });
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            kind: "error",
            message: e instanceof Error ? e.message : "load failed",
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, client]);

  if (state.kind === "loading") {
    return (
      <div className="card" data-testid="enrich-plan-loading">
        Loading enrich plan…
      </div>
    );
  }
  if (state.kind === "unavailable") {
    return (
      <div className="card" data-testid="enrich-plan-unavailable">
        <h3>Enrich Plan</h3>
        <p style={{ color: "var(--text-muted)" }}>
          {state.reason
            ?? "Enrich plan is not available for this run yet."}
        </p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="enrich-plan-error">
        <h3>Enrich Plan</h3>
        <p style={{ color: "var(--error-fg)" }}>
          Couldn't load enrich plan: {state.message}
        </p>
      </div>
    );
  }
  return <EnrichPlanContent plan={state.plan} />;
}

function EnrichPlanContent({ plan }: { plan: PostCompileEnrichPlanPayload }) {
  const banners = bannersForEnrichPlan(plan);
  return (
    <div data-testid="enrich-plan-panel">
      {banners.length > 0 && (
        <div data-testid="enrich-plan-banners">
          {banners.map((b, i) => (
            <div
              key={i}
              className={`banner banner--${b.kind}`}
              data-testid={b.testid}
            >
              {b.message}
            </div>
          ))}
        </div>
      )}
      <div className="card" data-testid="enrich-plan-card">
        <h3>Enrich Plan</h3>
        <dl className="kv">
          <dt>Recommendation</dt>
          <dd>
            <span
              className={`badge enrich-rec enrich-rec--${plan.overall_recommendation}`}
            >
              {recommendationLabel(plan.overall_recommendation)}
            </span>
          </dd>
          <dt>Decision source</dt>
          <dd>{decisionSourceLabel(plan.decision_source)}</dd>
          <dt>Reasons</dt>
          <dd>
            {plan.reasons.length === 0 ? "—" : (
              <ul className="bullet-list">
                {plan.reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            )}
          </dd>
          <dt>Recommended tasks</dt>
          <dd>
            {plan.recommended_tasks.length === 0 ? "—" : (
              <div className="cap-pills">
                {plan.recommended_tasks.map((t) => (
                  <span key={t} className="badge cap-pill">
                    {taskLabel(t)}
                  </span>
                ))}
              </div>
            )}
          </dd>
          <dt>Skipped tasks</dt>
          <dd>
            {plan.skipped_tasks.length === 0 ? "—" : (
              <div className="cap-pills">
                {plan.skipped_tasks.map((t) => (
                  <span key={t} className="badge cap-pill cap-pill--muted">
                    {taskLabel(t)}
                  </span>
                ))}
              </div>
            )}
          </dd>
          {plan.blocking_issues.length > 0 && (
            <>
              <dt>Blocking issues</dt>
              <dd>
                <ul className="bullet-list">
                  {plan.blocking_issues.map((b, i) => <li key={i}>{b}</li>)}
                </ul>
              </dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}
