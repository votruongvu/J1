/**
 * Documents page — the document-centric replacement for the
 * legacy run-list. Each row is a user-facing file (`Bridge Report.pdf`)
 * with its knowledge state badge, current-result summary, and the
 * server-computed available actions.
 *
 * The action matrix lives on the server (see
 * `j1.documents.projector.compute_available_actions`); this page
 * just iterates `availableActions` and renders whatever's there.
 * Adding/removing actions backend-side is a no-FE-change operation.
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { Banner } from "@/components/Banner";
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
  DocumentListItem,
} from "@/types/documents";
import type { ProjectContext, Toast } from "@/types/ui";

interface DocumentsPageProps {
  ctx: ProjectContext;
  onOpenDocument: (documentId: string) => void;
  onNewDocument: () => void;
  pushToast?: (toast: Omit<Toast, "id">) => void;
}

export function DocumentsPage({
  ctx, onOpenDocument, onNewDocument, pushToast,
}: DocumentsPageProps) {
  const client = useClient();
  const ready = !!ctx.tenant && !!ctx.project;

  const [items, setItems] = useState<DocumentListItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  // Pending-action target document. Drives the confirmation dialogs
  // — null means "no dialog open". Per the spec's section 10, detach
  // + remove get dedicated confirmation copy.
  const [pendingDetach, setPendingDetach] = useState<DocumentListItem | null>(null);
  const [pendingRemove, setPendingRemove] = useState<DocumentListItem | null>(null);
  const [busy, setBusy] = useState<string | null>(null);  // document_id of in-flight action

  const load = useCallback(async () => {
    if (!ready) {
      setItems(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await client.listDocuments();
      setItems(result);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 500;
      const message = e instanceof Error ? e.message : "Failed to load documents.";
      setError({ status, message });
    } finally {
      setLoading(false);
    }
  }, [client, ready]);

  useEffect(() => { void load(); }, [load]);

  const handleAction = async (
    document: DocumentListItem, action: DocumentAction,
  ) => {
    if (action === "view") {
      onOpenDocument(document.documentId);
      return;
    }
    // Detach + remove require confirmation per spec section 10.
    if (action === "detach") {
      setPendingDetach(document);
      return;
    }
    if (action === "remove") {
      setPendingRemove(document);
      return;
    }
    // Attach + reindex don't need a confirmation dialog — both are
    // reversible from the operator's perspective.
    setBusy(document.documentId);
    try {
      if (action === "attach") {
        await client.attachDocument(document.documentId);
        pushToast?.({ kind: "success", title: "Document attached to knowledge base" });
      } else if (action === "reindex") {
        const r = await client.reindexDocument(document.documentId);
        pushToast?.({
          kind: "success",
          title: "Re-index started",
          body: `New run: ${r.reindexRunId.slice(0, 12)}`,
        });
      } else if (action === "refresh_enrich") {
        const r = await client.refreshEnrichDocument(
          document.documentId,
        );
        pushToast?.({
          kind: "success",
          title: "Refresh enrichment started",
          body: `Reusing compile from ${
            r.reusedCompileFromRunId.slice(0, 12)
          }; new run: ${r.refreshRunId.slice(0, 12)}`,
        });
      } else if (action === "resume") {
        // Resume operates on a run, not the document. The detail
        // page is where this lives; the list-level button just
        // takes the user there.
        onOpenDocument(document.documentId);
        return;
      }
      await load();
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : "Action failed");
      pushToast?.({ kind: "error", title: `${action} failed`, body: msg });
    } finally {
      setBusy(null);
    }
  };

  const confirmDetach = async () => {
    if (!pendingDetach) return;
    const doc = pendingDetach;
    setPendingDetach(null);
    setBusy(doc.documentId);
    try {
      await client.detachDocument(doc.documentId);
      pushToast?.({
        kind: "success",
        title: "Document detached from knowledge base",
      });
      await load();
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : "Detach failed");
      pushToast?.({ kind: "error", title: "Detach failed", body: msg });
    } finally {
      setBusy(null);
    }
  };

  const confirmRemove = async () => {
    if (!pendingRemove) return;
    const doc = pendingRemove;
    setPendingRemove(null);
    setBusy(doc.documentId);
    try {
      await client.removeDocument(doc.documentId);
      pushToast?.({
        kind: "success",
        title: "Document removed from knowledge base",
      });
      await load();
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.message} (HTTP ${e.status})`
        : (e instanceof Error ? e.message : "Remove failed");
      pushToast?.({ kind: "error", title: "Remove failed", body: msg });
    } finally {
      setBusy(null);
    }
  };

  if (!ready) {
    return (
      <div className="documents-page">
        <Banner kind="info" title="Set tenant and project">
          Set the tenant and project headers in the context bar to load
          documents.
        </Banner>
      </div>
    );
  }

  return (
    <div className="documents-page">
      <header className="documents-page__header">
        <div>
          <h2 className="documents-page__title">Documents</h2>
          <p className="documents-page__subtitle">
            Files in this project's knowledge base. Manage the
            lifecycle here — attach, detach, re-index, or remove.
          </p>
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={onNewDocument}
        >
          Upload document
        </button>
      </header>

      {error && (
        <Banner kind="err" title={`Failed to load (HTTP ${error.status})`}>
          {error.message}
        </Banner>
      )}

      {loading && !items && (
        <div className="documents-page__loading">Loading documents…</div>
      )}

      {items && items.length === 0 && !loading && (
        <Banner kind="info" title="No documents yet">
          Upload your first document to seed this project's knowledge base.
        </Banner>
      )}

      {items && items.length > 0 && (
        <ul className="documents-list">
          {items.map((doc) => (
            <DocumentRow
              key={doc.documentId}
              document={doc}
              busy={busy === doc.documentId}
              onAction={(action) => handleAction(doc, action)}
            />
          ))}
        </ul>
      )}

      <ConfirmDetachDialog
        document={pendingDetach}
        onConfirm={() => void confirmDetach()}
        onCancel={() => setPendingDetach(null)}
      />
      <ConfirmRemoveDialog
        document={pendingRemove}
        onConfirm={() => void confirmRemove()}
        onCancel={() => setPendingRemove(null)}
      />
    </div>
  );
}


interface DocumentRowProps {
  document: DocumentListItem;
  busy: boolean;
  onAction: (action: DocumentAction) => void;
}

function DocumentRow({ document, busy, onAction }: DocumentRowProps) {
  // The "view" action shows up in every state's matrix; the rest
  // depend on knowledge state + active run + compile checkpoint.
  // We split it out so it becomes the row's click target while the
  // other actions render as explicit buttons.
  const actions = document.availableActions.filter((a) => a !== "view");
  return (
    <li className="documents-list__row" data-testid="document-row">
      <button
        type="button"
        className="documents-list__open"
        onClick={() => onAction("view")}
      >
        <div className="documents-list__primary">
          <span className="documents-list__name">{document.displayName}</span>
          <KnowledgeStateBadge state={document.knowledgeState} />
        </div>
        <div className="documents-list__meta">
          <ResultStatusBadge summary={document.currentResultSummary} />
          <span className="documents-list__id mono">
            {document.documentId.slice(0, 12)}…
          </span>
          {document.runHistorySummary.length > 0 && (
            <span className="documents-list__history">
              {document.runHistorySummary.length} run{
                document.runHistorySummary.length === 1 ? "" : "s"
              }
            </span>
          )}
          {document.updatedAt && (
            <span className="documents-list__updated">
              updated {relativeTime(document.updatedAt)}
            </span>
          )}
        </div>
      </button>
      <div className="documents-list__actions">
        {actions.map((action) => (
          <ActionButton
            key={action}
            action={action}
            disabled={busy}
            onClick={() => onAction(action)}
          />
        ))}
      </div>
    </li>
  );
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
      title={meta.title}
    >
      {meta.label}
    </button>
  );
}


// Centralised action-label config. Single source of truth for the
// UI strings; matches the operator-friendly wording from the spec.
// Adding a new action server-side requires extending this map — but
// failing soft (showing the raw string) would be a poor UX, so we
// gate the type system instead.
const ACTION_META: Record<DocumentAction, {
  label: string; variant: "primary" | "ghost" | "danger"; title: string;
}> = {
  view:    { label: "View",       variant: "ghost",  title: "Open document detail" },
  reindex: { label: "Re-index",   variant: "ghost",  title: "Rebuild knowledge for this document" },
  refresh_enrich: {
    label: "Refresh enrichment",
    variant: "ghost",
    title: (
      "Re-run enrichment + graph + index, reusing the previous "
      + "active run's compile output"
    ),
  },
  detach:  { label: "Detach",     variant: "ghost",  title: "Stop using this document for retrieval" },
  attach:  { label: "Attach",     variant: "primary", title: "Use this document for retrieval again" },
  remove:  { label: "Remove",     variant: "danger", title: "Remove this document's generated knowledge" },
  resume:  { label: "Continue",   variant: "primary", title: "Continue from the compiled result" },
};
