/**
 * Pure helpers for the Home dashboard.
 *
 * Every aggregation the dashboard renders is derived from the
 * document list returned by `client.listDocuments()`. No new
 * backend endpoint exists for "system status totals" — these
 * helpers fold the document-centric data into the dashboard
 * shape the operator actually wants to see ("X indexed, Y
 * failed, Z running, last successful at...").
 *
 * Side-effect-free + side-state-free so every rule is testable
 * with synthetic fixtures.
 */

import {
  COMPLETED_STATUSES,
  RUNNING_STATUSES,
  RUN_STATUS,
  type RunStatus,
} from "@/lib/constants/runStatus";
import type {
  DocumentListItem,
  DocumentRunSummary,
} from "@/types/documents";


/**
 * Snapshot of "is the knowledge base ready and healthy?" — the
 * question the Home page answers at a glance. Counts come from
 * the document list (each item's `knowledgeState` +
 * `currentResultSummary.status`).
 *
 * Field semantics:
 *
 *   `total`               — every document the user sees on the
 *                           Documents page (excludes `removed` —
 *                           those are tombstoned).
 *   `indexed`             — `knowledgeState=="attached"` AND the
 *                           current result succeeded. This is the
 *                           set the global search will hit.
 *   `failed`              — current result is FAILED. Distinct
 *                           from `running` and `indexed`; never
 *                           overlaps.
 *   `running`             — current result is in a running status
 *                           (per `RUNNING_STATUSES`).
 *   `detached`            — `knowledgeState=="detached"`. Visible
 *                           in lists but excluded from search.
 *   `lastSuccessfulAt`    — most recent `updatedAt` across
 *                           documents in the `indexed` bucket;
 *                           ISO 8601 string. `null` when none.
 *
 * Buckets are disjoint by design — the dashboard renders them as
 * separate counters and double-counting would mislead operators.
 */
export interface DocumentStatusSummary {
  total: number;
  indexed: number;
  failed: number;
  running: number;
  detached: number;
  lastSuccessfulAt: string | null;
}


function _statusOf(doc: DocumentListItem): string {
  return (doc.currentResultSummary?.status ?? "").toLowerCase();
}


function _isSucceeded(status: string): boolean {
  // Mirror the backend's "success" vocabulary: covers `succeeded`
  // and the legacy `completed` alias. We lowercase here because
  // the wire shape isn't case-stable across older runs.
  return COMPLETED_STATUSES.has(
    status.toUpperCase() as RunStatus,
  ) || status === "succeeded" || status === "succeeded_with_warnings";
}


function _isFailed(status: string): boolean {
  return status === "failed";
}


function _isRunning(status: string): boolean {
  return RUNNING_STATUSES.has(status.toUpperCase() as RunStatus)
    || status === "running"
    || status === "compiling"
    || status === "assessing";
}


export function aggregateDocumentStatus(
  documents: readonly DocumentListItem[],
): DocumentStatusSummary {
  let indexed = 0;
  let failed = 0;
  let running = 0;
  let detached = 0;
  let lastSuccessfulAt: string | null = null;

  for (const doc of documents) {
    if (doc.knowledgeState === "removed") continue;
    if (doc.knowledgeState === "detached") {
      detached += 1;
      continue;
    }
    const status = _statusOf(doc);
    if (_isSucceeded(status)) {
      indexed += 1;
      // ISO 8601 strings sort lexicographically; safe to compare
      // with `>` when both have the same zone offset (always Z on
      // the wire for our backend).
      if (
        doc.updatedAt != null
        && (lastSuccessfulAt === null || doc.updatedAt > lastSuccessfulAt)
      ) {
        lastSuccessfulAt = doc.updatedAt;
      }
    } else if (_isFailed(status)) {
      failed += 1;
    } else if (_isRunning(status)) {
      running += 1;
    }
  }

  return {
    total: documents.filter((d) => d.knowledgeState !== "removed").length,
    indexed,
    failed,
    running,
    detached,
    lastSuccessfulAt,
  };
}


/**
 * One row in the "Recent runs" panel. Flat shape — the panel
 * doesn't need to know which document each row came from
 * structurally, just to render the column.
 */
export interface RecentRunRow {
  runId: string;
  documentId: string;
  documentName: string;
  runType: DocumentRunSummary["runType"];
  status: string;
  startedAt: string | null;
  completedAt: string | null;
  /** Server-computed duration in milliseconds. Null when the run
   * hasn't completed yet OR when timestamps were missing. */
  durationMs: number | null;
}


function _computeDurationMs(
  startedAt: string | null,
  completedAt: string | null,
): number | null {
  if (!startedAt || !completedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(completedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  if (end < start) return null;
  return end - start;
}


/**
 * Collect the N most recent runs across every document.
 *
 * Each `DocumentListItem.runHistorySummary` carries the tail of
 * the document's runs (server-truncated). Flatten + sort by
 * `startedAt` descending. Runs without a `startedAt` sort to
 * the back (they're typically just-queued).
 */
export function collectRecentRuns(
  documents: readonly DocumentListItem[],
  limit: number = 5,
): RecentRunRow[] {
  const rows: RecentRunRow[] = [];
  for (const doc of documents) {
    for (const run of doc.runHistorySummary ?? []) {
      rows.push({
        runId: run.runId,
        documentId: doc.documentId,
        documentName: doc.displayName,
        runType: run.runType,
        status: run.status,
        startedAt: run.startedAt,
        completedAt: run.completedAt,
        durationMs: _computeDurationMs(run.startedAt, run.completedAt),
      });
    }
  }
  rows.sort((a, b) => {
    if (a.startedAt === null && b.startedAt === null) return 0;
    if (a.startedAt === null) return 1;
    if (b.startedAt === null) return -1;
    return a.startedAt > b.startedAt ? -1 : a.startedAt < b.startedAt ? 1 : 0;
  });
  return rows.slice(0, limit);
}


/**
 * One "Needs attention" item. The Home dashboard renders the
 * tuple as a single line: an icon-tinted banner with `message`
 * and a clickable `action` when set.
 *
 * `kind` drives the colour: `warn` is amber, `err` is red. The
 * dashboard never emits `info` here — informational state goes
 * in the status summary, not the attention list.
 */
export interface AttentionItem {
  id: string;
  kind: "warn" | "err";
  message: string;
}


/**
 * Build the operator-readable warnings list. Rules — order
 * matters because the dashboard renders them top-to-bottom:
 *
 *   1. NO active documents at all     → err  (search will not work)
 *   2. ANY document currently running → warn (transient — ETA)
 *   3. ANY failed runs                → err  (action needed)
 *   4. ANY attached document with no active snapshot → warn
 *      (the document is visible but search will skip it)
 *
 * Pinned in tests so a reorder is intentional.
 */
export function computeNeedsAttention(
  documents: readonly DocumentListItem[],
  summary: DocumentStatusSummary,
): readonly AttentionItem[] {
  const items: AttentionItem[] = [];

  if (summary.total > 0 && summary.indexed === 0) {
    items.push({
      id: "no-indexed",
      kind: "err",
      message:
        "No documents are currently indexed. Search will return no "
        + "results until at least one document finishes ingesting.",
    });
  }

  if (summary.running > 0) {
    items.push({
      id: "running",
      kind: "warn",
      message:
        summary.running === 1
          ? "1 document is currently being processed."
          : `${summary.running} documents are currently being processed.`,
    });
  }

  if (summary.failed > 0) {
    items.push({
      id: "failed",
      kind: "err",
      message:
        summary.failed === 1
          ? "1 document failed to ingest. Inspect Recent Runs to retry."
          : `${summary.failed} documents failed to ingest. Inspect Recent Runs to retry.`,
    });
  }

  // Attached documents with no active snapshot — they're still
  // listed but search will silently exclude them.
  const attachedNotIndexed = documents.filter(
    (d) =>
      d.knowledgeState === "attached"
      && d.activeSnapshotId == null
      && !_isFailed(_statusOf(d)),
  );
  if (attachedNotIndexed.length > 0) {
    items.push({
      id: "attached-not-indexed",
      kind: "warn",
      message:
        attachedNotIndexed.length === 1
          ? "1 attached document has no indexed snapshot yet."
          : `${attachedNotIndexed.length} attached documents have no indexed snapshot yet.`,
    });
  }

  return items;
}


/**
 * Render a short, business-friendly description of the run-type
 * tag. Used in the Recent Runs panel + the Document Detail run
 * history so operators see "Initial ingest" instead of
 * ``"initial"``. Single source of truth — both call sites import
 * from here so a new ``RunType`` value gets a label in one place.
 *
 * Accepts ``string`` so a forward-compat FE that hasn't been
 * re-built for a brand-new BE ``RunType`` value still renders the
 * raw wire string instead of crashing.
 */
export function runTypeLabel(runType: string): string {
  switch (runType) {
    case "initial": return "Initial ingest";
    case "reindex": return "Reindex";
    case "resume": return "Resume";
    case "retry": return "Retry";
    case "validation": return "Validation";
    case "refresh_enrich": return "Refresh enrichment";
    case "run_domain_enrichment": return "Domain Enrichment";
    default: return runType;
  }
}


/**
 * Format a duration in milliseconds as the most operator-readable
 * unit. Examples: `42 ms`, `1.2 s`, `15 s`, `2m 30s`, `1h 5m`.
 * `null` input renders as `"—"`.
 */
export function formatDuration(durationMs: number | null): string {
  if (durationMs == null) return "—";
  if (durationMs < 1000) return `${durationMs} ms`;
  const seconds = durationMs / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const remSec = Math.round(seconds - minutes * 60);
  if (minutes < 60) {
    return remSec > 0 ? `${minutes}m ${remSec}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remMin = minutes - hours * 60;
  return remMin > 0 ? `${hours}h ${remMin}m` : `${hours}h`;
}


// Re-export the unused-on-purpose constants so the module's
// dependency graph is explicit (the linter would otherwise drop
// the imports). RUN_STATUS is consumed by other modules importing
// from here in tests; keeping the re-export keeps the API surface
// clean.
export { RUN_STATUS };
