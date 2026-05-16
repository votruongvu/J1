/**
 * Enrichment Result panel.
 *
 * Renders the typed `EnrichmentResult` overlay via
 * `GET /ingestion-runs/{id}/enrichment-result`. Surfaces:
 * - the run-level enrichment status (succeeded / warnings /
 * failed / skipped) — with operator-readable reason copy;
 * - per-module outcomes (which modules ran, which were skipped,
 * warnings/errors per module);
 * - what enrichment ACTUALLY ADDED (metadata, terminology,
 * validation findings) so operators can see real value, not
 * just "ran" badges.
 *
 * Treats `skipped` as a neutral outcome (not a failure). Empty /
 * skeleton module outputs render as a neutral "no enrichment
 * artefacts" message — never pretend rich results when modules
 * are still skeletons.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Typewriter } from "@/components/Typewriter";

import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import type {
  EnrichmentModuleOutcomePayload,
  EnrichmentResultPayload,
  RunEnrichmentResultResponse,
} from "@/types/review";


interface EnrichmentResultPanelProps {
  runId: string;
  latestEvent?: ProgressEvent | null;
}


type PanelState =
  | { kind: "loading" }
  | { kind: "unavailable"; reason: string | null }
  | {
      kind: "ready";
      resp: RunEnrichmentResultResponse;
      plan: EnrichmentResultPayload;
    }
  | { kind: "error"; message: string };


export function EnrichmentResultPanel({
  runId,
  latestEvent,
}: EnrichmentResultPanelProps) {
  const client = useClient();
  const [state, setState] = useState<PanelState>({ kind: "loading" });

  const loadPlan = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunEnrichmentResult(runId);
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

  // STEP_STARTED is included because enrichment_result is persisted
  // by a silent activity (no SSE event) between enrich's
  // step.completed and the next stage's step.started. Without
  // STEP_STARTED the overlay only fills on later step.completed
  // events near terminal.
  useEffect(() => {
    if (!latestEvent) return;
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_STARTED,
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
      <div className="card" data-testid="enrichment-result-loading">
        <div className="card__body">Loading enrichment overlay…</div>
      </div>
    );
  }
  if (state.kind === "unavailable") {
    return (
      <div className="card" data-testid="enrichment-result-unavailable">
        <div className="card__body">
          <h3>Enrichment Overlay</h3>
          <p style={{ color: "var(--text-muted)" }}>
            {state.reason
              ?? "Enrichment overlay is not available for this run yet."}
          </p>
        </div>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="enrichment-result-error">
        <div className="card__body">
          <h3>Enrichment Overlay</h3>
          <p style={{ color: "var(--error-fg)" }}>
            Couldn't load enrichment overlay: {state.message}
          </p>
        </div>
      </div>
    );
  }
  return <EnrichmentResultContent plan={state.plan} />;
}


function EnrichmentResultContent({
  plan,
}: {
  plan: EnrichmentResultPayload;
}) {
  const status = plan.status ?? "succeeded";
  const outcomes = plan.module_outcomes ?? [];
  const warnings = plan.warnings ?? [];
  const errors = plan.errors ?? [];
  const docMeta = plan.document_metadata ?? {};
  const terminology = plan.terminology ?? [];

  const isSkipped = status === "skipped";
  const hasAdditions = useMemo(
    () => Object.keys(docMeta).length > 0 || terminology.length > 0,
    [docMeta, terminology],
  );

  return (
    <div
      className="card run-panel enrichment-result-panel"
      data-testid="enrichment-result-panel"
    >
      <div className="run-panel__head">
        <h3>Enrichment Overlay</h3>
        <span
          className="run-panel__source"
          data-testid="enrichment-result-source"
        >
          {statusLabel(status)}
        </span>
      </div>

      <div
        className={`banner enrichment-banner enrichment-banner--${status}`}
        data-testid="enrichment-result-banner"
      >
        <strong>Post-compile domain enrichment: {statusLabel(status)}</strong>
        {plan.reason ? (
          <span>
            {" "}— <Typewriter text={plan.reason} speed={140} cursor />
          </span>
        ) : null}
      </div>

      {isSkipped ? (
        <p
          className="run-panel__hint"
          data-testid="enrichment-result-skipped-explainer"
        >
          <Typewriter
            text="Post-compile domain enrichment was skipped for this run. The base compile output is unchanged and remains the source of truth for downstream consumers."
            speed={140}
            cursor
          />
        </p>
      ) : null}

      {!isSkipped && outcomes.length > 0 && (
        <div
          className="run-panel__subsection"
          data-testid="enrichment-result-modules-section"
        >
          <div className="run-panel__label">Modules</div>
          <ul
            className="module-outcome-list"
            data-testid="enrichment-result-modules"
          >
            {outcomes.map((o) => (
              <ModuleOutcomeRow key={o.module_id} outcome={o} />
            ))}
          </ul>
        </div>
      )}

      {!isSkipped && (
        <div
          className="run-panel__subsection"
          data-testid="enrichment-result-additions-section"
        >
          <div className="run-panel__label">What enrichment added</div>
          <div data-testid="enrichment-result-additions">
            {hasAdditions ? (
              <ul className="bullet-list">
                {Object.keys(docMeta).length > 0 && (
                  <li>
                    Document metadata: {Object.keys(docMeta).length}{" "}
                    field{Object.keys(docMeta).length === 1 ? "" : "s"}
                  </li>
                )}
                {terminology.length > 0 && (
                  <li>
                    Terminology entries: {terminology.length}
                  </li>
                )}
              </ul>
            ) : (
              <span
                style={{ color: "var(--text-muted)" }}
                data-testid="enrichment-result-no-additions"
              >
                No enrichment artefacts were produced for this run.
              </span>
            )}
          </div>
        </div>
      )}

      {warnings.length > 0 && (
        <div
          className="run-panel__subsection"
          data-testid="enrichment-result-warnings-section"
        >
          <div className="run-panel__label">Warnings</div>
          <ul
            className="bullet-list"
            data-testid="enrichment-result-warnings"
            style={{ color: "var(--warning-fg)" }}
          >
            {warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
      {errors.length > 0 && (
        <div
          className="run-panel__subsection"
          data-testid="enrichment-result-errors-section"
        >
          <div className="run-panel__label">Errors</div>
          <ul
            className="bullet-list"
            data-testid="enrichment-result-errors"
            style={{ color: "var(--error-fg)" }}
          >
            {errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}

      {/* Phase 3B (2026-05-16): cross-link to the Knowledge Memory
          status section on Document Detail. We deliberately don't
          re-fetch the memory status here — the enrichment panel
          stays focused on enrichment outcomes. The Document Detail
          page is the canonical place to inspect whether enrichment
          insights actually got projected into Knowledge Memory. */}
      {!isSkipped && (
        <p
          className="run-panel__hint enrichment-result__memory-note muted"
          data-testid="enrichment-result-memory-note"
        >
          Domain insights from this enrichment can be projected into
          Knowledge Memory. The Knowledge Memory section on the
          Document Detail page shows whether memory has been updated
          with these insights.
        </p>
      )}
    </div>
  );
}


function ModuleOutcomeRow({
  outcome,
}: {
  outcome: EnrichmentModuleOutcomePayload;
}) {
  return (
    <li
      className={`module-outcome module-outcome--${outcome.status}`}
      data-testid={`enrichment-module-${outcome.module_id}`}
    >
      <span className="module-outcome__name">
        {humaniseModuleId(outcome.module_id)}
      </span>
      <span
        className={`badge module-outcome__status module-outcome__status--${outcome.status}`}
      >
        {moduleStatusLabel(outcome.status)}
      </span>
      {outcome.reason ? (
        <span className="module-outcome__reason">{outcome.reason}</span>
      ) : null}
    </li>
  );
}


function statusLabel(status: string): string {
  switch (status) {
    case "succeeded":
      return "completed";
    case "succeeded_with_warnings":
      return "completed with warnings";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    default:
      return status;
  }
}


function moduleStatusLabel(status: string): string {
  switch (status) {
    case "run":
      return "ran";
    case "partial":
      return "partial";
    case "skipped":
      return "skipped";
    case "failed":
      return "failed";
    default:
      return status;
  }
}


function humaniseModuleId(id: string): string {
  return id
    .split("_")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}
