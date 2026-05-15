/**
 * Document detail page — full document view with run history.
 *
 * Renders three sections, top to bottom:
 *
 *  1. Document summary (filename, knowledge state, current
 *     result, latest version pointer).
 *  2. Current result roll-up (compile / enrichment / validation
 *     statuses from the active run's metadata).
 *  3. Run history table — every attempt under this document, most
 *     recent first, with the active run highlighted.
 *
 * Action buttons live in the header and call the same handlers as
 * the list page (so behavior stays consistent across the two
 * entry points).
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { Banner } from "@/components/Banner";
import { Icon } from "@/components/icons";
import { useClient } from "@/lib/hooks/useClient";
import { relativeTime } from "@/lib/format";
import {
  ConfirmDetachDialog,
  ConfirmRemoveDialog,
} from "./documents/DocumentLifecycleDialogs";
import {
  KnowledgeStateBadge,
  ResultStatusBadge,
} from "./documents/DocumentBadges";
import { TestActiveKnowledgePanel } from "./documents/TestActiveKnowledgePanel";
import { ManualActionsPanel } from "./documents/ManualActionsPanel";
import { AssessmentPlanDialog } from "./upload/AssessmentPlanDialog";
import type {
  DocumentAction,
  DocumentDetail,
  DocumentListItem,
  DocumentSnapshotSummary,
} from "@/types/documents";
import { runTypeLabel } from "./home/home-dashboard-helpers";
import type {
  AssessmentPlanResponse,
  ExecutionProfileId,
} from "@/types/execution-profile";
import type { ProjectContext, Toast } from "@/types/ui";

interface DocumentDetailPageProps {
  documentId: string;
  ctx: ProjectContext;
  onBack: () => void;
  onOpenRun: (runId: string) => void;
  pushToast?: (toast: Omit<Toast, "id">) => void;
}

export function DocumentDetailPage({
  documentId, ctx, onBack, onOpenRun, pushToast,
}: DocumentDetailPageProps) {
  const client = useClient();
  const ready = !!ctx.tenant && !!ctx.project;

  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [snapshots, setSnapshots] = useState<DocumentSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const [pendingDetach, setPendingDetach] = useState<DocumentListItem | null>(null);
  const [pendingRemove, setPendingRemove] = useState<DocumentListItem | null>(null);
  const [busy, setBusy] = useState(false);

  // Re-index now flows through AssessmentPlanDialog so the user picks
  // an execution profile before the new run is dispatched — same
  // two-step contract as the first-time upload path. `pendingReindex`
  // is true once the dialog is open; the plan fetch resolves into
  // `planResponse` (or `planError` on failure).
  const [pendingReindex, setPendingReindex] = useState(false);
  const [planResponse, setPlanResponse] =
    useState<AssessmentPlanResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!ready) return;
    setLoading(true);
    setError(null);
    try {
      // Snapshot list is parallel-fetched so the Candidate Knowledge
      // / Active Knowledge sections render state badges without a
      // second round-trip. Failure here is non-fatal — the page
      // still works without snapshot state; the badges just don't
      // show. Deployments that don't wire snapshot_service return
      // 503 and the client adapter converts that to an empty list.
      const [result, snapsResult] = await Promise.all([
        client.getDocumentDetail(documentId),
        client.listDocumentSnapshots(documentId).catch(() => []),
      ]);
      setDetail(result);
      setSnapshots(snapsResult);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 500;
      const message = e instanceof Error ? e.message : "Failed to load document.";
      setError({ status, message });
    } finally {
      setLoading(false);
    }
  }, [client, documentId, ready]);

  useEffect(() => { void load(); }, [load]);

  // The dialogs accept a `DocumentListItem` — we adapt the
  // DocumentDetail to the same shape rather than duplicating the
  // dialog code with a second variant.
  const detailAsListItem = (d: DocumentDetail): DocumentListItem => ({
    documentId: d.documentId,
    displayName: d.displayName,
    knowledgeState: d.knowledgeState,
    activeSnapshotId: d.activeSnapshotId,
    latestVersionId: d.latestVersionId,
    createdAt: d.createdAt,
    updatedAt: d.updatedAt,
    removedAt: d.removedAt,
    currentResultSummary: d.currentResultSummary,
    availableActions: d.availableActions,
    runHistorySummary: d.runHistory,
  });

  const handleAction = async (action: DocumentAction) => {
    if (!detail) return;
    if (action === "view") return;  // already viewing
    if (action === "detach") {
      setPendingDetach(detailAsListItem(detail));
      return;
    }
    if (action === "remove") {
      setPendingRemove(detailAsListItem(detail));
      return;
    }
    if (action === "reindex") {
      // Open the assessment-plan picker first — re-index now mirrors
      // the upload flow, so the user always picks an execution
      // profile before the run is dispatched. Fetch the plan in the
      // background; the dialog renders "Analysing…" until it lands.
      setPendingReindex(true);
      setPlanResponse(null);
      setPlanError(null);
      void (async () => {
        try {
          const plan = await client.getDocumentAssessmentPlan(documentId);
          setPlanResponse(plan);
        } catch (e) {
          setPlanError(
            e instanceof Error
              ? e.message
              : "Could not analyse this document.",
          );
        }
      })();
      return;
    }
    setBusy(true);
    try {
      if (action === "attach") {
        await client.attachDocument(documentId);
        pushToast?.({ kind: "success", title: "Document attached" });
      }
      await load();
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : `${action} failed`);
      pushToast?.({ kind: "error", title: `${action} failed`, body: msg });
    } finally {
      setBusy(false);
    }
  };

  const onReindexConfirm = async (selectedProfile: ExecutionProfileId) => {
    setPendingReindex(false);
    setPlanResponse(null);
    setPlanError(null);
    setBusy(true);
    try {
      const r = await client.reindexDocument(documentId, selectedProfile);
      pushToast?.({
        kind: "success",
        title: "Building new candidate snapshot",
        body: (
          `Current active knowledge stays live while the new ` +
          `candidate is built. Run ${r.reindexRunId.slice(0, 12)}.`
        ),
      });
      await load();
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : "reindex failed");
      pushToast?.({ kind: "error", title: "reindex failed", body: msg });
    } finally {
      setBusy(false);
    }
  };

  const onReindexCancel = () => {
    setPendingReindex(false);
    setPlanResponse(null);
    setPlanError(null);
  };

  const performDetach = async () => {
    setPendingDetach(null);
    setBusy(true);
    try {
      await client.detachDocument(documentId);
      pushToast?.({ kind: "success", title: "Document detached" });
      await load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Detach failed";
      pushToast?.({ kind: "error", title: "Detach failed", body: msg });
    } finally {
      setBusy(false);
    }
  };

  const performRemove = async () => {
    setPendingRemove(null);
    setBusy(true);
    try {
      await client.removeDocument(documentId);
      pushToast?.({ kind: "success", title: "Document removed" });
      await load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Remove failed";
      pushToast?.({ kind: "error", title: "Remove failed", body: msg });
    } finally {
      setBusy(false);
    }
  };

  if (loading && !detail) {
    return (
      <div className="document-detail">
        <BackLink onBack={onBack} />
        <div className="muted">Loading document…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="document-detail">
        <BackLink onBack={onBack} />
        <Banner kind="err" title={`Failed to load (HTTP ${error.status})`}>
          {error.message}
        </Banner>
      </div>
    );
  }

  if (!detail) return null;

  const otherActions = detail.availableActions.filter((a) => a !== "view");
  const summary = detail.currentResultSummary;
  // Snapshot-centric model: the visibility key is
  // ``activeSnapshotId``. The "active run" is the run that
  // *produced* that snapshot — preferred lookup is
  // ``r.targetSnapshotId === activeSnapshotId`` since the producing
  // run is the load-bearing one for delete protection / promotion
  // copy. Falls back to ``r.isActive`` on legacy runs that don't
  // carry ``targetSnapshotId`` yet.
  const activeSnapshotId = detail.activeSnapshotId;
  // Snapshot lookup map keyed by snapshot id. Empty when the
  // snapshot service isn't wired or the second fetch failed; in
  // that case the Candidate / Active sections fall back to
  // run-status-only rendering.
  const snapshotsById: Record<string, DocumentSnapshotSummary> =
    Object.fromEntries(snapshots.map((s) => [s.snapshotId, s]));
  const activeSnapshot = activeSnapshotId
    ? snapshotsById[activeSnapshotId] ?? null
    : null;
  const activeProducingRun = activeSnapshotId
    ? detail.runHistory.find(
        (r) => r.targetSnapshotId === activeSnapshotId,
      ) ?? detail.runHistory.find((r) => r.isActive)
    : detail.runHistory.find((r) => r.isActive);
  const activeRunId = activeProducingRun?.runId ?? null;
  // A "candidate" is any run that has *not* produced the active
  // snapshot. Surfaces in the Candidate Knowledge section so users
  // understand the blue/green model: current active stays
  // queryable while a new candidate is being built / awaiting
  // promotion. Most recent first; in-flight runs sort to the top
  // because runHistory is already started_at desc.
  const candidateRuns = detail.runHistory.filter(
    (r) => r.runId !== activeRunId,
  );
  const inflightCandidate = candidateRuns.find((r) =>
    ["running", "paused", "cancelling", "assessing", "created"].includes(
      (r.status || "").toLowerCase(),
    ),
  );

  return (
    <div className="document-detail">
      <div className="run-hero doc-hero">
        <div className="run-hero__top">
          <div>
            <div className="run-hero__crumb">
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  onBack();
                }}
              >
                <Icon.ChevronLeft className="icon-sm" /> Documents
              </a>
              <span>·</span>
              <span className="mono">
                {ctx.tenant} / {ctx.project}
              </span>
            </div>
            <h2>
              <span className="run-hero__doc-icon">
                <Icon.File className="icon" />
              </span>
              <span
                style={{
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {detail.displayName}
              </span>
              <KnowledgeStateBadge state={detail.knowledgeState} />
              <ResultStatusBadge summary={summary} />
            </h2>
            <div className="run-hero__id">{detail.documentId}</div>
          </div>
          <div className="run-hero__actions">
            {otherActions.map((action) => (
              <ActionButton
                key={action}
                action={action}
                disabled={busy}
                onClick={() => handleAction(action)}
              />
            ))}
          </div>
        </div>

        <div className="run-stats">
          <div className="run-stats__item">
            <label>Overall</label>
            <div className="v">
              <ResultStatusBadge summary={summary} />
            </div>
          </div>
          <div className="run-stats__item">
            <label>Compile</label>
            <div className="vsmall">{summary.compileStatus ?? "—"}</div>
          </div>
          <div className="run-stats__item">
            <label>Enrichment</label>
            <div className="vsmall">{summary.enrichmentStatus ?? "—"}</div>
          </div>
          <div className="run-stats__item">
            <label>Validation</label>
            <div className="vsmall">{summary.validationStatus ?? "—"}</div>
          </div>
          <div className="run-stats__item">
            <label
              title={
                "The canonical visibility key. The Active Knowledge " +
                "Snapshot is what global query reads from. A new " +
                "re-index builds a Candidate Snapshot; the active " +
                "snapshot stays live until promotion."
              }
            >
              Active snapshot
            </label>
            <div className="v mono">
              {activeSnapshotId
                ? `${activeSnapshotId.slice(0, 12)}…`
                : "none yet"}
            </div>
          </div>
          <div className="run-stats__item">
            <label>Updated</label>
            <div className="vsmall">
              {detail.updatedAt ? relativeTime(detail.updatedAt) : "—"}
            </div>
          </div>
        </div>
      </div>

      {detail.knowledgeState === "detached" && (
        <Banner kind="warn" title="This document is detached from project knowledge">
          Excluded from global query. Manual testing on this page is
          still available for inspection. Attach it again to bring it
          back into project query scope.
        </Banner>
      )}
      {detail.knowledgeState === "removed" && (
        <Banner kind="err" title="This document has been removed">
          The generated knowledge for this document has been removed from
          active indexes. Re-attaching requires re-uploading the file.
        </Banner>
      )}
      {summary.failureCode && (
        <Banner kind="err" title="Last run failure">
          <code className="mono">{summary.failureCode}</code>
        </Banner>
      )}

      <section className="document-detail__section">
        <h3>Active Knowledge Snapshot</h3>
        <ActiveKnowledgePanel
          detail={detail}
          producingRun={activeProducingRun ?? null}
          activeSnapshot={activeSnapshot}
        />
      </section>

      <section className="document-detail__section">
        <h3>Manual Actions</h3>
        {/* TODO: Thread ``recommendedNextSteps`` once Document Detail
            fetches the latest AssessmentDecision (or equivalent LLM
            advanced-assessment payload). The panel already accepts
            the prop and renders a "Recommended" pill; we just don't
            fetch the data on this page yet. Until then the panel
            renders correctly with the prop undefined. */}
        <ManualActionsPanel
          client={client}
          documentId={detail.documentId}
          activeSnapshotId={detail.activeSnapshotId}
          hasInflightRun={!!inflightCandidate}
          onOpenRun={onOpenRun}
        />
      </section>

      <section className="document-detail__section">
        <h3>Test Active Knowledge</h3>
        <p className="muted document-detail__hint">
          Ask a question scoped to this document's active snapshot.
          Run is not a query scope — the request uses
          <code className="mono"> document_active </code>
          and the backend resolves the active snapshot id.
        </p>
        <TestActiveKnowledgePanel
          documentId={detail.documentId}
          activeSnapshotId={detail.activeSnapshotId}
          producingRunId={activeRunId}
          onOpenRun={onOpenRun}
        />
      </section>

      {(inflightCandidate || candidateRuns.length > 0) && (
        <section className="document-detail__section">
          <h3>
            Candidate Knowledge
            {inflightCandidate && (
              <span className="run-history-row__active-badge">
                in progress
              </span>
            )}
          </h3>
          <p className="muted document-detail__hint">
            J1 keeps the current snapshot active while a new candidate
            is built. The candidate only replaces the current snapshot
            after it completes and passes the configured checks.
          </p>
          <CandidateKnowledgeList
            runs={candidateRuns}
            snapshotsById={snapshotsById}
            onOpenRun={onOpenRun}
          />
        </section>
      )}

      <section className="document-detail__section">
        <h3>
          Processing History
          <span className="document-detail__count">
            ({detail.runHistory.length})
          </span>
        </h3>
        <p className="muted document-detail__hint">
          Each row is one processing run. The snapshot it produced is
          the queryable unit; the run itself is the execution log.
        </p>
        <RunHistoryTable
          runs={detail.runHistory}
          activeRunId={activeRunId}
          activeSnapshotId={activeSnapshotId}
          onOpenRun={onOpenRun}
        />
      </section>

      <ConfirmDetachDialog
        document={pendingDetach}
        onConfirm={() => void performDetach()}
        onCancel={() => setPendingDetach(null)}
      />
      <ConfirmRemoveDialog
        document={pendingRemove}
        onConfirm={() => void performRemove()}
        onCancel={() => setPendingRemove(null)}
      />
      {pendingReindex && (
        <AssessmentPlanDialog
          filename={detail.displayName}
          plan={planResponse}
          loadError={planError}
          onConfirm={(profile) => void onReindexConfirm(profile)}
          onCancel={onReindexCancel}
        />
      )}
    </div>
  );
}


function BackLink({ onBack }: { onBack: () => void }) {
  return (
    <div className="document-detail__back">
      <a
        href="#"
        className="run-hero__crumb"
        onClick={(e) => {
          e.preventDefault();
          onBack();
        }}
      >
        <Icon.ChevronLeft className="icon-sm" /> Documents
      </a>
    </div>
  );
}


export function ActiveKnowledgePanel({
  detail, producingRun, activeSnapshot,
}: {
  detail: DocumentDetail;
  producingRun: DocumentDetail["runHistory"][number] | null;
  activeSnapshot: DocumentSnapshotSummary | null;
}) {
  if (!detail.activeSnapshotId) {
    return (
      <p className="muted">
        No active snapshot yet — re-index this document to build the
        first knowledge version. Until a snapshot is promoted, this
        document is not queryable.
      </p>
    );
  }
  const queryable = detail.knowledgeState === "attached";
  return (
    <div className="active-knowledge-panel">
      <dl className="active-knowledge-panel__grid">
        <div>
          <dt>Snapshot ID</dt>
          <dd className="mono" title={detail.activeSnapshotId}>
            {detail.activeSnapshotId.slice(0, 16)}…
          </dd>
        </div>
        <div>
          <dt>State</dt>
          <dd>
            {activeSnapshot?.state ? (
              <SnapshotStateBadge state={activeSnapshot.state} />
            ) : (
              <span className="muted">—</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Produced by run</dt>
          <dd className="mono">
            {producingRun ? (
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                }}
                title={producingRun.runId}
              >
                {producingRun.runId.slice(0, 12)}…
                {producingRun.displayVersion && (
                  <span className="run-history-row__version-chip">
                    v{producingRun.displayVersion}
                  </span>
                )}
              </a>
            ) : (
              <span className="muted">—</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Promoted</dt>
          <dd className="vsmall">
            {activeSnapshot?.promotedAt
              ? relativeTime(activeSnapshot.promotedAt)
              : <span className="muted">—</span>}
          </dd>
        </div>
        <div>
          <dt>Knowledge state</dt>
          <dd><KnowledgeStateBadge state={detail.knowledgeState} /></dd>
        </div>
        <div>
          <dt>Queryable in global scope</dt>
          <dd className={queryable ? "ok" : "muted"}>
            {queryable ? "Yes" : "No"}
          </dd>
        </div>
      </dl>
    </div>
  );
}


export function CandidateKnowledgeList({
  runs, snapshotsById, onOpenRun,
}: {
  runs: DocumentDetail["runHistory"];
  snapshotsById: Record<string, DocumentSnapshotSummary>;
  onOpenRun: (runId: string) => void;
}) {
  if (runs.length === 0) {
    return (
      <p className="muted">
        No candidate snapshots in this document's history.
      </p>
    );
  }
  return (
    <ul className="candidate-knowledge-list">
      {runs.slice(0, 5).map((run) => {
        const snap = run.targetSnapshotId
          ? snapshotsById[run.targetSnapshotId] ?? null
          : null;
        return (
        <li key={run.runId} className="candidate-knowledge-list__row">
          <div className="candidate-knowledge-list__head">
            <RunStatusPill status={run.status} />
            {snap?.state && <SnapshotStateBadge state={snap.state} />}
            <span className="mono" title={run.runId}>
              {run.runId.slice(0, 12)}…
            </span>
            <span className="muted">{runTypeLabel(run.runType)}</span>
            {run.displayVersion && (
              <span className="run-history-row__version-chip">
                v{run.displayVersion}
              </span>
            )}
          </div>
          <div className="candidate-knowledge-list__meta">
            <span>
              <strong>Candidate snapshot: </strong>
              <code className="mono">
                {run.targetSnapshotId
                  ? `${run.targetSnapshotId.slice(0, 16)}…`
                  : "—"}
              </code>
            </span>
            <span className="muted">
              Not used by global query until promoted.
            </span>
          </div>
          <div className="candidate-knowledge-list__actions">
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => onOpenRun(run.runId)}
            >
              View processing run
            </button>
          </div>
        </li>
        );
      })}
    </ul>
  );
}


/**
 * Snapshot state badge — building / ready / superseded / failed.
 * Tone matches the snapshot's lifecycle role:
 *   building     → neutral / running
 *   ready        → ok (active or promotable)
 *   superseded   → muted (kept for audit)
 *   failed       → err
 */
export function SnapshotStateBadge({
  state,
}: {
  state: NonNullable<DocumentSnapshotSummary["state"]>;
}) {
  const labels: Record<typeof state, string> = {
    building: "Building",
    ready: "Ready",
    superseded: "Superseded",
    failed: "Failed",
  };
  const tones: Record<typeof state, string> = {
    building: "running",
    ready: "ok",
    superseded: "neutral",
    failed: "err",
  };
  return (
    <span className={`badge badge--${tones[state]}`}>
      {labels[state]}
    </span>
  );
}


function RunHistoryTable({
  runs, activeRunId, activeSnapshotId, onOpenRun,
}: {
  runs: DocumentDetail["runHistory"];
  activeRunId: string | null;
  activeSnapshotId: string | null;
  onOpenRun: (runId: string) => void;
}) {
  if (runs.length === 0) {
    return <p className="muted">No runs yet for this document.</p>;
  }
  return (
    <table className="run-history-table">
      <thead>
        <tr>
          <th>Run</th>
          <th>Type</th>
          <th>Status</th>
          <th>Produced snapshot</th>
          <th>Started</th>
          <th>Completed</th>
          <th className="run-history-row__action"></th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const isActiveProducer =
            run.runId === activeRunId
            || (!!activeSnapshotId
              && run.targetSnapshotId === activeSnapshotId);
          return (
          <tr
            key={run.runId}
            className={isActiveProducer ? "run-history-row--active" : ""}
          >
            <td>
              <span className="run-history-row__id">
                <span className="run-history-row__id-text">
                  {run.runId.slice(0, 12)}…
                </span>
                {isActiveProducer && (
                  <span
                    className="run-history-row__active-badge"
                    title="This run produced the document's active knowledge snapshot."
                  >
                    active snapshot
                  </span>
                )}
                {run.displayVersion && (
                  <span
                    className="run-history-row__version-chip"
                    title="Operator-facing version (DDMMYYYY-NN)"
                  >
                    v{run.displayVersion}
                  </span>
                )}
              </span>
            </td>
            <td className="run-history-row__type">{runTypeLabel(run.runType)}</td>
            <td>
              <RunStatusPill status={run.status} />
            </td>
            <td className="run-history-row__snapshot mono">
              {run.targetSnapshotId ? (
                <span
                  title={run.targetSnapshotId}
                  className={isActiveProducer ? "ok" : ""}
                >
                  {run.targetSnapshotId.slice(0, 12)}…
                </span>
              ) : (
                <span className="muted">—</span>
              )}
            </td>
            <td className="run-history-row__time">
              {run.startedAt ? relativeTime(run.startedAt) : "—"}
            </td>
            <td className="run-history-row__time">
              {run.completedAt ? relativeTime(run.completedAt) : "—"}
            </td>
            <td className="run-history-row__action">
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => onOpenRun(run.runId)}
              >
                Open
              </button>
            </td>
          </tr>
          );
        })}
      </tbody>
    </table>
  );
}


function RunStatusPill({ status }: { status: string }) {
  const normalised = (status || "").toLowerCase();
  let tone: "ok" | "warn" | "err" | "running" | "default" = "default";
  let label = status || "—";
  if (
    normalised === "succeeded" || normalised === "completed"
  ) {
    tone = "ok";
    label = "succeeded";
  } else if (
    normalised === "succeeded_with_warnings"
    || normalised === "completed_with_warnings"
  ) {
    tone = "warn";
    label = "warnings";
  } else if (normalised === "failed") {
    tone = "err";
    label = "failed";
  } else if (normalised === "cancelled" || normalised === "canceled") {
    tone = "warn";
    label = "cancelled";
  } else if (normalised) {
    tone = "running";
    label = normalised.replace(/_/g, " ");
  }
  const className =
    tone === "default"
      ? "run-history-status"
      : `run-history-status run-history-status--${tone}`;
  return <span className={className}>{label}</span>;
}


function ActionButton({
  action, disabled, onClick,
}: {
  action: DocumentAction;
  disabled: boolean;
  onClick: () => void;
}) {
  const meta = ACTION_META[action];
  return (
    <button
      type="button"
      className={`btn btn--${meta.variant}`}
      onClick={onClick}
      disabled={disabled}
    >
      {meta.label}
    </button>
  );
}


const ACTION_META: Record<DocumentAction, {
  label: string; variant: "primary" | "ghost" | "danger";
}> = {
  view:    { label: "View",       variant: "ghost"   },
  reindex: { label: "Re-Index Document", variant: "primary" },
  detach:  { label: "Detach from Knowledge", variant: "ghost" },
  attach:  { label: "Attach to Knowledge",   variant: "primary" },
  remove:  { label: "Remove Knowledge", variant: "danger"  },
};
