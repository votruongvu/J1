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
import type {
  DocumentAction,
  DocumentDetail,
  DocumentListItem,
} from "@/types/documents";
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const [pendingDetach, setPendingDetach] = useState<DocumentListItem | null>(null);
  const [pendingRemove, setPendingRemove] = useState<DocumentListItem | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!ready) return;
    setLoading(true);
    setError(null);
    try {
      const result = await client.getDocumentDetail(documentId);
      setDetail(result);
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
    activeRunId: d.activeRunId,
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
    setBusy(true);
    try {
      if (action === "attach") {
        await client.attachDocument(documentId);
        pushToast?.({ kind: "success", title: "Document attached" });
      } else if (action === "reindex") {
        const r = await client.reindexDocument(documentId);
        pushToast?.({
          kind: "success",
          title: "Re-index started",
          body: `New run: ${r.reindexRunId.slice(0, 12)}`,
        });
      } else if (action === "refresh_enrich") {
        const r = await client.refreshEnrichDocument(documentId);
        pushToast?.({
          kind: "success",
          title: "Refresh enrichment started",
          body: `Reusing compile from ${
            r.reusedCompileFromRunId.slice(0, 12)
          }; new run: ${r.refreshRunId.slice(0, 12)}`,
        });
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
            <label>Active run</label>
            <div className="v mono">
              {detail.activeRunId
                ? `${detail.activeRunId.slice(0, 12)}…`
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
        <Banner kind="warn" title="This document is detached">
          J1 is not using this document for search, answers, validation,
          or domain context. Attach it again to re-enable retrieval.
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
        <h3>
          Run history
          <span className="document-detail__count">
            ({detail.runHistory.length})
          </span>
        </h3>
        <RunHistoryTable
          runs={detail.runHistory}
          activeRunId={detail.activeRunId}
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


function RunHistoryTable({
  runs, activeRunId, onOpenRun,
}: {
  runs: DocumentDetail["runHistory"];
  activeRunId: string | null;
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
          <th>Started</th>
          <th>Completed</th>
          <th className="run-history-row__action"></th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr
            key={run.runId}
            className={run.runId === activeRunId ? "run-history-row--active" : ""}
          >
            <td>
              <span className="run-history-row__id">
                <span className="run-history-row__id-text">
                  {run.runId.slice(0, 12)}…
                </span>
                {run.runId === activeRunId && (
                  <span className="run-history-row__active-badge">active</span>
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
            <td className="run-history-row__type">{run.runType}</td>
            <td>
              <RunStatusPill status={run.status} />
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
        ))}
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
  refresh_enrich: {
    label: "Refresh enrichment",
    variant: "ghost",
  },
  detach:  { label: "Detach from Knowledge", variant: "ghost" },
  attach:  { label: "Attach to Knowledge",   variant: "primary" },
  remove:  { label: "Remove from Knowledge", variant: "danger"  },
};
