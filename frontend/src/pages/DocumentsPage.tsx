/**
 * Documents page — list view.
 *
 * Each row is a single click-through to the document detail page;
 * lifecycle actions (re-index, detach, remove) live there, not here.
 * That keeps destructive operations one step away from the list
 * scroll and gives the row the room to surface richer status info
 * inline (knowledge state, stage health, active run, last update).
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { Banner } from "@/components/Banner";
import { useClient } from "@/lib/hooks/useClient";
import { relativeTime } from "@/lib/format";
import {
  KnowledgeStateBadge,
  ResultStatusBadge,
} from "./documents/DocumentBadges";
import type { DocumentListItem } from "@/types/documents";
import type { ProjectContext } from "@/types/ui";

interface DocumentsPageProps {
  ctx: ProjectContext;
  onOpenDocument: (documentId: string) => void;
  onNewDocument: () => void;
}

export function DocumentsPage({
  ctx, onOpenDocument, onNewDocument,
}: DocumentsPageProps) {
  const client = useClient();
  const ready = !!ctx.tenant && !!ctx.project;

  const [items, setItems] = useState<DocumentListItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);

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
            Files in this project's knowledge base. Open a document to
            re-index, detach, or remove it.
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
              onOpen={() => onOpenDocument(doc.documentId)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}


interface DocumentRowProps {
  document: DocumentListItem;
  onOpen: () => void;
}

function DocumentRow({ document, onOpen }: DocumentRowProps) {
  const summary = document.currentResultSummary;
  const failureCode = summary.failureCode;
  const runCount = document.runHistorySummary.length;
  const activeRun = document.runHistorySummary.find((r) => r.isActive);
  const lastRun =
    activeRun ?? document.runHistorySummary[0] ?? null;
  return (
    <li className="documents-list__row" data-testid="document-row">
      <button
        type="button"
        className="documents-list__open"
        onClick={onOpen}
        aria-label={`Open ${document.displayName}`}
      >
        <div className="documents-list__primary">
          <span className="documents-list__name">{document.displayName}</span>
          <KnowledgeStateBadge state={document.knowledgeState} />
          <ResultStatusBadge summary={summary} />
        </div>

        <div className="documents-list__stages">
          <StageChip label="Compile" status={summary.compileStatus} />
          <StageChip label="Enrich" status={summary.enrichmentStatus} />
          <StageChip label="Validation" status={summary.validationStatus} />
          {failureCode && (
            <span
              className="documents-list__chip documents-list__chip--err"
              title="Last run failure code"
            >
              {failureCode}
            </span>
          )}
        </div>

        <div className="documents-list__meta">
          <span className="documents-list__id mono" title={document.documentId}>
            {document.documentId.slice(0, 12)}…
          </span>
          <span className="documents-list__sep" aria-hidden>·</span>
          <span className="documents-list__runs">
            {runCount === 0
              ? "no runs yet"
              : `${runCount} run${runCount === 1 ? "" : "s"}`}
          </span>
          {lastRun?.startedAt && (
            <>
              <span className="documents-list__sep" aria-hidden>·</span>
              <span className="documents-list__last-run">
                last run {relativeTime(lastRun.startedAt)}
              </span>
            </>
          )}
          {document.updatedAt && (
            <>
              <span className="documents-list__sep" aria-hidden>·</span>
              <span className="documents-list__updated">
                updated {relativeTime(document.updatedAt)}
              </span>
            </>
          )}
        </div>
      </button>
    </li>
  );
}


function StageChip({
  label, status,
}: {
  label: string;
  status: string | null;
}) {
  const tone = chipTone(status);
  const display = status ?? "—";
  return (
    <span
      className={`documents-list__chip documents-list__chip--${tone}`}
      title={`${label}: ${display}`}
    >
      <span className="documents-list__chip-label">{label}</span>
      <span className="documents-list__chip-value">
        {display.replace(/_/g, " ")}
      </span>
    </span>
  );
}


type ChipTone = "ok" | "warn" | "err" | "running" | "neutral";


function chipTone(status: string | null): ChipTone {
  if (!status) return "neutral";
  const s = status.toLowerCase();
  if (s === "succeeded" || s === "completed" || s === "passed") return "ok";
  if (
    s === "succeeded_with_warnings"
    || s === "completed_with_warnings"
    || s === "passed_with_warnings"
    || s === "warnings"
  ) {
    return "warn";
  }
  if (s === "failed" || s === "error") return "err";
  if (s === "cancelled" || s === "canceled" || s === "inconclusive") {
    return "warn";
  }
  if (s === "none" || s === "skipped") return "neutral";
  return "running";
}


