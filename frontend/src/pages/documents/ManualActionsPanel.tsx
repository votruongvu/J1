/**
 * ManualActionsPanel — post-index Manual Actions surface on the
 * Document Detail page.
 *
 * One card per action returned by ``GET /documents/{id}/manual-actions``.
 * Only ``run_domain_enrichment`` is wired today; every other action
 * renders as a disabled "Coming soon" card. The panel hides itself
 * when the FE is in legacy mode (``manualActionsEnabled === false``).
 *
 * Triggering ``run_domain_enrichment`` calls
 * ``POST /documents/{id}/manual-actions/run-domain-enrichment`` and
 * surfaces the queued / running / succeeded / failed lifecycle by
 * polling ``GET /ingestion-runs/{id}`` — the same surface the run
 * detail page uses, so we never invent a parallel status model.
 */

import { useCallback, useEffect, useState } from "react";

import { Banner } from "@/components/Banner";
import { ApiError, type IngestionClient } from "@/lib/api/client";
import { manualActionsEnabled } from "@/lib/constants/feature-flags";
import type { ManualActionDescriptor } from "@/types/execution-profile";
import type { IngestionRun } from "@/types/ingestion";

export const RUN_DOMAIN_ENRICHMENT_ID = "run_domain_enrichment";


/** Terminal status set for an IngestionRun — once the run lands in
 * one of these states the polling loop stops and the summary
 * renders. Matches ``IngestionRun.is_terminal()`` on the backend. */
const TERMINAL_STATUSES = new Set([
  "succeeded",
  "succeeded_with_warnings",
  "failed",
  "cancelled",
  "requires_human_review",
]);


export interface ManualActionsPanelProps {
  client: IngestionClient;
  documentId: string;
  /** ``activeSnapshotId`` from the document detail. Drives eligibility:
   * without an active snapshot there's no baseline compile to enrich. */
  activeSnapshotId: string | null;
  /** When true, another run is currently in flight against this
   * document — Run Domain Enrichment must refuse the click. */
  hasInflightRun: boolean;
  /** ``recommended_next_steps`` carried from the most recent LLM
   * Advanced Assessment. When this includes ``run_domain_enrichment``
   * we highlight the card with a "Recommended" pill — but we NEVER
   * auto-run it. */
  recommendedNextSteps?: readonly string[];
  /** Polling cadence in ms. Defaulted for production; tests override
   * to a fast tick so they don't wait on a real timer. */
  pollIntervalMs?: number;
  /** Hook for the run-detail page link. Optional — when omitted the
   * panel just renders the run id as text. */
  onOpenRun?: (runId: string) => void;
}


type LifecycleState =
  | { kind: "idle" }
  | { kind: "confirming" }
  | { kind: "starting" }
  | { kind: "running"; runId: string; run: IngestionRun | null }
  | { kind: "terminal"; runId: string; run: IngestionRun }
  | { kind: "error"; message: string };


export function ManualActionsPanel({
  client,
  documentId,
  activeSnapshotId,
  hasInflightRun,
  recommendedNextSteps,
  pollIntervalMs = 2500,
  onOpenRun,
}: ManualActionsPanelProps) {
  // The picker is responsible for hiding itself when the deployment
  // turned the whole surface off — Document Detail callers don't
  // have to remember to.
  const enabled = manualActionsEnabled;
  const [actions, setActions] = useState<ManualActionDescriptor[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [lifecycle, setLifecycle] = useState<LifecycleState>({ kind: "idle" });

  // Fetch the vocabulary once on mount + after a successful trigger
  // so the panel reflects the latest deployment / per-action status.
  const loadActions = useCallback(async () => {
    setLoadError(null);
    try {
      const out = await client.listDocumentManualActions(documentId);
      setActions(out.actions);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load manual actions");
    }
  }, [client, documentId]);

  useEffect(() => {
    if (!enabled) return;
    void loadActions();
  }, [enabled, loadActions]);

  // Poll the run while the manual action is in flight. The cleanup
  // function stops the loop when the lifecycle leaves "running" —
  // either we transition to "terminal" or the operator navigates
  // away.
  // TODO: Replace lifecycle polling with SSE subscription for
  // long-running manual actions. The backend already streams
  // ``/ingestion-runs/{id}/events/stream``; switching to it would
  // drop the steady poll traffic for multi-minute enrichments. Kept
  // as polling for the demo build because it's the same pattern the
  // run-detail page uses today.
  useEffect(() => {
    if (lifecycle.kind !== "running") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const run = await client.getRun(lifecycle.runId);
        if (cancelled) return;
        if (TERMINAL_STATUSES.has(run.status.toLowerCase())) {
          setLifecycle({ kind: "terminal", runId: lifecycle.runId, run });
          return;
        }
        setLifecycle({ kind: "running", runId: lifecycle.runId, run });
      } catch {
        // Transient — keep polling. A genuinely missing run id
        // would manifest as 404 here, but the run was just created
        // synchronously so we trust the response and ignore.
      }
    };
    void tick();
    const handle = setInterval(() => void tick(), pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [client, lifecycle, pollIntervalMs]);

  if (!enabled) return null;

  const onTrigger = async () => {
    setLifecycle({ kind: "starting" });
    try {
      const resp = await client.runDomainEnrichment(documentId);
      setLifecycle({ kind: "running", runId: resp.manualActionRunId, run: null });
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : "Failed to start manual action");
      setLifecycle({ kind: "error", message: msg });
    }
  };

  const onConfirm = () => setLifecycle({ kind: "confirming" });
  const onCancel = () => setLifecycle({ kind: "idle" });

  return (
    <div className="manual-actions-panel" data-testid="manual-actions-panel">
      <p className="muted document-detail__hint">
        Operator-triggered advanced steps. None of these run
        automatically — the default index path stays lightweight. Each
        click here may incur LLM cost; the panel never auto-chains
        actions.
      </p>

      {loadError !== null && (
        <Banner kind="err" title="Couldn't load manual actions">
          {loadError}
        </Banner>
      )}

      {actions === null && loadError === null && (
        <p className="muted">Loading manual actions…</p>
      )}

      {actions !== null && (
        <ul className="manual-actions-panel__list">
          {actions.map((a) => (
            <ManualActionCard
              key={a.id}
              action={a}
              activeSnapshotId={activeSnapshotId}
              hasInflightRun={hasInflightRun}
              lifecycle={lifecycle}
              recommended={
                (recommendedNextSteps ?? []).includes(a.id)
              }
              onConfirm={a.id === RUN_DOMAIN_ENRICHMENT_ID ? onConfirm : undefined}
            />
          ))}
        </ul>
      )}

      {lifecycle.kind === "confirming" && (
        <ConfirmRunDomainEnrichmentDialog
          onConfirm={() => void onTrigger()}
          onCancel={onCancel}
        />
      )}

      {(lifecycle.kind === "starting"
        || lifecycle.kind === "running"
        || lifecycle.kind === "terminal"
        || lifecycle.kind === "error") && (
        <ManualActionStatus
          lifecycle={lifecycle}
          onDismiss={() => {
            setLifecycle({ kind: "idle" });
            void loadActions();
          }}
          onOpenRun={onOpenRun}
        />
      )}
    </div>
  );
}


function ManualActionCard({
  action, activeSnapshotId, hasInflightRun, lifecycle, recommended, onConfirm,
}: {
  action: ManualActionDescriptor;
  activeSnapshotId: string | null;
  hasInflightRun: boolean;
  lifecycle: LifecycleState;
  recommended: boolean;
  onConfirm: (() => void) | undefined;
}) {
  const isRunDomainEnrichment = action.id === RUN_DOMAIN_ENRICHMENT_ID;
  const inFlight = lifecycle.kind === "starting"
    || lifecycle.kind === "running";

  // Eligibility precedence (most specific first → coarsest):
  //   1. server status not "available"   → coming soon / disabled-by-deployment
  //   2. document has no active snapshot → "index the document first"
  //   3. other run in flight             → "another action is running"
  //   4. THIS manual action already mid-flight → "in progress"
  let disabledReason: string | null = null;
  if (action.status === "not_implemented") {
    disabledReason = "Coming soon — endpoint not yet implemented.";
  } else if (action.status === "disabled") {
    disabledReason = "Disabled by deployment settings.";
  } else if (isRunDomainEnrichment) {
    if (activeSnapshotId === null) {
      disabledReason = "Document must be indexed first (no active snapshot).";
    } else if (hasInflightRun) {
      disabledReason = "Another run is currently in flight against this document.";
    } else if (inFlight) {
      disabledReason = "This manual action is already in progress.";
    }
  } else if (onConfirm === undefined) {
    // Future "available" actions that the FE hasn't been taught to
    // trigger yet — render disabled with the same Coming soon
    // copy so we don't ship a button that 404s.
    disabledReason = "Coming soon — FE handler not wired.";
  }

  const disabled = disabledReason !== null;
  const testIdPrefix = `manual-action-card-${action.id}`;

  return (
    <li
      className={
        "manual-actions-panel__card"
        + (recommended ? " manual-actions-panel__card--recommended" : "")
      }
      data-testid={testIdPrefix}
    >
      <div className="manual-actions-panel__card-header">
        <strong>{action.label}</strong>
        {recommended && (
          <span
            className="manual-actions-panel__recommended-pill"
            data-testid={`${testIdPrefix}-recommended-pill`}
          >
            Recommended
          </span>
        )}
      </div>
      <p className="manual-actions-panel__card-description">
        {action.description}
      </p>
      <small className="muted">{action.costNote}</small>
      <div className="manual-actions-panel__card-actions">
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={disabled}
          aria-disabled={disabled || undefined}
          onClick={() => onConfirm?.()}
          data-testid={`${testIdPrefix}-trigger`}
        >
          {action.label}
        </button>
        {disabledReason && (
          <small
            className="muted"
            data-testid={`${testIdPrefix}-reason`}
          >
            {disabledReason}
          </small>
        )}
      </div>
    </li>
  );
}


function ConfirmRunDomainEnrichmentDialog({
  onConfirm, onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      className="modal-backdrop"
      onClick={onCancel}
      data-testid="manual-actions-confirm-backdrop"
    >
      <div
        className="modal-card"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <h3>Run Domain Enrichment?</h3>
        <p>
          This will start a new candidate snapshot that reuses the
          active snapshot's compile artifacts and runs the active
          domain pack's enrichers. It will make multiple LLM calls
          and may take several minutes. The current active snapshot
          stays live; the new candidate only replaces it on success.
        </p>
        <div className="modal__actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onCancel}
            data-testid="manual-actions-confirm-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onConfirm}
            data-testid="manual-actions-confirm"
          >
            Run Domain Enrichment
          </button>
        </div>
      </div>
    </div>
  );
}


function ManualActionStatus({
  lifecycle, onDismiss, onOpenRun,
}: {
  lifecycle: LifecycleState;
  onDismiss: () => void;
  onOpenRun: ((runId: string) => void) | undefined;
}) {
  if (lifecycle.kind === "error") {
    return (
      <div
        className="manual-actions-panel__status"
        data-testid="manual-action-status-error"
      >
        <Banner kind="err" title="Failed to start Run Domain Enrichment">
          {lifecycle.message}
        </Banner>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onDismiss}
        >
          Dismiss
        </button>
      </div>
    );
  }
  if (lifecycle.kind === "starting") {
    return (
      <div
        className="manual-actions-panel__status"
        data-testid="manual-action-status-starting"
      >
        <p>Queueing Run Domain Enrichment…</p>
      </div>
    );
  }
  if (lifecycle.kind === "running") {
    const stage = lifecycle.run?.currentStage ?? "queued";
    return (
      <div
        className="manual-actions-panel__status"
        data-testid="manual-action-status-running"
      >
        <p>
          Running Domain Enrichment — current stage: <strong>{stage}</strong>.
        </p>
        <small className="muted">
          Run id: <span className="mono">{lifecycle.runId}</span>
        </small>
        {onOpenRun && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => onOpenRun(lifecycle.runId)}
          >
            View processing run
          </button>
        )}
      </div>
    );
  }
  // terminal
  const status = lifecycle.run.status.toLowerCase();
  const ok = status === "succeeded" || status === "succeeded_with_warnings";
  return (
    <div
      className="manual-actions-panel__status"
      data-testid={`manual-action-status-${ok ? "succeeded" : "failed"}`}
    >
      <Banner
        kind={ok ? "ok" : "err"}
        title={
          ok
            ? "Domain Enrichment finished"
            : "Domain Enrichment failed"
        }
      >
        <p>
          Run <span className="mono">{lifecycle.runId}</span> ended with status
          {" "}<strong>{lifecycle.run.status}</strong>
          {lifecycle.run.failureCode && (
            <> · code <code className="mono">{lifecycle.run.failureCode}</code></>
          )}
          .
        </p>
        {lifecycle.run.warningCount > 0 && (
          <p className="muted">
            {lifecycle.run.warningCount} warning(s) reported.
          </p>
        )}
      </Banner>
      <div className="manual-actions-panel__status-actions">
        {onOpenRun && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => onOpenRun(lifecycle.runId)}
          >
            View processing run
          </button>
        )}
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onDismiss}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
