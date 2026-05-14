/**
 * Validation Tab.
 *
 * Hosts two surfaces, both compact:
 *
 *  1. **Imported Test Cases** (the auxiliary helper). Upload a CSV
 *     per document, run it against the document's latest succeeded
 *     run, see only summary cards + per-question status. Each row
 *     has an "Open in Manual Test Query" action that prefills the
 *     ManualQueryConsole textarea below.
 *
 *  2. **Manual Test Query** (the detailed inspection tool). Owned by
 *     ``ManualQueryConsole``. The imperative handle lets the Imported
 *     section drop a question into it without modifying the console's
 *     internal contract.
 *
 * No generated test cases, no answer judging, no draft/approve UI —
 * those were deleted in the 2026-05-14 product change.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { ApiError } from "@/lib/api/client";
import type {
  ImportedTestCaseExecution,
  ImportedTestCaseOverallStatus,
  ImportedTestCaseResult,
  ImportedTestCaseSet,
  ImportedTestCaseStatus,
  ImportedTestCaseSummary,
} from "@/types/review";

import {
  ManualQueryConsole,
  type ManualQueryConsoleHandle,
} from "./ManualQueryConsole";

interface ValidationTabProps {
  runId: string;
  documentId: string | null;
  /** Snapshot this run produced — drives the snapshot_explicit default
   * for ManualQueryConsole. ``null`` for legacy runs predating the
   * snapshot model; the console falls back to document_active. */
  targetSnapshotId: string | null;
}

export function ValidationTab(
  { runId, documentId, targetSnapshotId }: ValidationTabProps,
) {
  const client = useClient();
  const manualQueryRef = useRef<ManualQueryConsoleHandle | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [importedSet, setImportedSet] = useState<ImportedTestCaseSet | null>(
    null,
  );
  const [latestExecution, setLatestExecution] = useState<
    ImportedTestCaseExecution | null
  >(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!documentId) return;
    setLoading(true);
    setError(null);
    try {
      const [set, execution] = await Promise.all([
        client.getImportedTestCases(documentId),
        client.getImportedTestCaseExecution(documentId),
      ]);
      setImportedSet(set);
      setLatestExecution(execution);
    } catch (e) {
      setError(_errorMessage(e, "Failed to load imported test cases."));
    } finally {
      setLoading(false);
    }
  }, [client, documentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onFilePicked = useCallback(
    async (file: File) => {
      if (!documentId) return;
      setImporting(true);
      setError(null);
      setInfo(null);
      try {
        const set = await client.importTestCases(documentId, file);
        setImportedSet(set);
        // Import always replaces the prior execution snapshot on the
        // server. Reflect that in local state so stale summary cards
        // don't linger.
        setLatestExecution(null);
        setInfo(`Imported ${set.cases.length} test case(s).`);
      } catch (e) {
        setError(_errorMessage(e, "Import failed."));
      } finally {
        setImporting(false);
        if (fileInputRef.current) {
          fileInputRef.current.value = "";
        }
      }
    },
    [client, documentId],
  );

  const onExecute = useCallback(async () => {
    if (!documentId) return;
    if (!importedSet || importedSet.cases.length === 0) {
      setError("Import a CSV first.");
      return;
    }
    setExecuting(true);
    setError(null);
    setInfo(null);
    try {
      const execution = await client.executeImportedTestCases(documentId);
      setLatestExecution(execution);
    } catch (e) {
      setError(_errorMessage(e, "Execution failed."));
    } finally {
      setExecuting(false);
    }
  }, [client, documentId, importedSet]);

  const onDelete = useCallback(async () => {
    if (!documentId) return;
    setError(null);
    setInfo(null);
    try {
      await client.deleteImportedTestCases(documentId);
      setImportedSet(null);
      setLatestExecution(null);
      setInfo("Imported test cases cleared.");
    } catch (e) {
      setError(_errorMessage(e, "Delete failed."));
    }
  }, [client, documentId]);

  const onOpenInManualQuery = useCallback((question: string) => {
    manualQueryRef.current?.loadQuestion(question);
  }, []);

  const resultsByCaseId = new Map<string, ImportedTestCaseResult>();
  for (const r of latestExecution?.results ?? []) {
    resultsByCaseId.set(r.testCaseId, r);
  }

  return (
    <div className="validation-tab" style={{ display: "grid", gap: 16 }}>
      <ImportedTestCasesSection
        documentId={documentId}
        importedSet={importedSet}
        latestExecution={latestExecution}
        resultsByCaseId={resultsByCaseId}
        loading={loading}
        importing={importing}
        executing={executing}
        fileInputRef={fileInputRef}
        onFilePicked={onFilePicked}
        onExecute={onExecute}
        onDelete={onDelete}
        onOpenInManualQuery={onOpenInManualQuery}
      />

      {error && (
        <div className="banner banner--err" role="alert">{error}</div>
      )}
      {info && !error && (
        <div className="banner banner--ok" role="status">{info}</div>
      )}

      <ManualQueryConsole
        ref={manualQueryRef}
        runId={runId}
        documentId={documentId}
        targetSnapshotId={targetSnapshotId}
      />
    </div>
  );
}

// ---- Imported Test Cases section -----------------------------------

interface ImportedTestCasesSectionProps {
  documentId: string | null;
  importedSet: ImportedTestCaseSet | null;
  latestExecution: ImportedTestCaseExecution | null;
  resultsByCaseId: Map<string, ImportedTestCaseResult>;
  loading: boolean;
  importing: boolean;
  executing: boolean;
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>;
  onFilePicked: (file: File) => void | Promise<void>;
  onExecute: () => void | Promise<void>;
  onDelete: () => void | Promise<void>;
  onOpenInManualQuery: (question: string) => void;
}

function ImportedTestCasesSection({
  documentId,
  importedSet,
  latestExecution,
  resultsByCaseId,
  loading,
  importing,
  executing,
  fileInputRef,
  onFilePicked,
  onExecute,
  onDelete,
  onOpenInManualQuery,
}: ImportedTestCasesSectionProps) {
  if (!documentId) {
    return (
      <div className="card">
        <div className="card__body">
          Imported test cases are scoped to a single document. This
          run isn't tagged with a document_id, so the import surface
          is unavailable. Use Manual Test Query below for one-off
          questions.
        </div>
      </div>
    );
  }

  const cases = importedSet?.cases ?? [];
  const summary = latestExecution?.summary ?? null;
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      void onFilePicked(file);
    }
  };

  return (
    <div className="card">
      <div
        className="card__header"
        style={{ display: "flex", justifyContent: "space-between" }}
      >
        <div>
          <strong>Imported Test Cases</strong>
          <div className="muted" style={{ fontSize: 12 }}>
            Auxiliary validation helper — upload a CSV per document,
            execute against the latest succeeded run, and get a
            quick confidence summary.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            onChange={handleFileChange}
            style={{ display: "none" }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            title="Replaces the current imported set"
          >
            {importing ? "Importing…" : (
              cases.length > 0 ? "Re-import CSV" : "Import CSV"
            )}
          </button>
          <button
            type="button"
            onClick={() => void onExecute()}
            disabled={executing || cases.length === 0}
            title="Run every imported question against the latest succeeded run"
          >
            {executing ? "Running…" : "Run"}
          </button>
          {cases.length > 0 && (
            <button
              type="button"
              onClick={() => void onDelete()}
              disabled={importing || executing}
              title="Clear the imported set"
            >
              Clear
            </button>
          )}
        </div>
      </div>
      <div className="card__body" style={{ display: "grid", gap: 12 }}>
        {loading && !importedSet ? (
          <div>Loading imported test cases…</div>
        ) : cases.length === 0 ? (
          <div className="muted">
            No imported test cases yet. The CSV needs a{" "}
            <code>question</code> column; optional columns are{" "}
            <code>expected_answer</code>, <code>expected_sources</code>
            , <code>test_type</code>, <code>notes</code>. Every
            import replaces the prior set.
          </div>
        ) : (
          <>
            <SummaryCards summary={summary} caseCount={cases.length} />
            <CompactCasesTable
              cases={importedSet!.cases}
              resultsByCaseId={resultsByCaseId}
              onOpenInManualQuery={onOpenInManualQuery}
            />
            {summary === null && (
              <div className="muted" style={{ fontSize: 12 }}>
                Click <strong>Run</strong> to execute the imported
                set against the document's latest succeeded run.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ---- Summary cards -------------------------------------------------

function SummaryCards({
  summary,
  caseCount,
}: {
  summary: ImportedTestCaseSummary | null;
  caseCount: number;
}) {
  const cards: Array<{ label: string; value: string | number }> = [
    {
      label: "Total",
      value: summary?.total ?? caseCount,
    },
    {
      label: "Answered",
      value: summary ? summary.answered : "—",
    },
    {
      label: "With Sources",
      value: summary ? summary.withSources : "—",
    },
    {
      label: "Scope Issues",
      value: summary ? summary.scopeIssues : "—",
    },
    {
      label: "Overall",
      value: summary ? _overallLabel(summary.overall) : "Not run",
    },
  ];
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(5, minmax(0, 1fr))",
        gap: 8,
      }}
    >
      {cards.map((card) => (
        <div
          key={card.label}
          className="card"
          style={{ padding: 8, textAlign: "center" }}
        >
          <div className="muted" style={{ fontSize: 11 }}>
            {card.label}
          </div>
          <div style={{ fontWeight: 600, fontSize: 16 }}>{card.value}</div>
        </div>
      ))}
    </div>
  );
}

// ---- Compact table -------------------------------------------------

function CompactCasesTable({
  cases,
  resultsByCaseId,
  onOpenInManualQuery,
}: {
  cases: ImportedTestCaseSet["cases"];
  resultsByCaseId: Map<string, ImportedTestCaseResult>;
  onOpenInManualQuery: (q: string) => void;
}) {
  return (
    <table className="table" style={{ width: "100%" }}>
      <thead>
        <tr>
          <th style={{ textAlign: "left" }}>Question</th>
          <th>Status</th>
          <th>Sources</th>
          <th>Scope</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {cases.map((c) => {
          const result = resultsByCaseId.get(c.testCaseId);
          const status: ImportedTestCaseStatus = result?.status ?? "not_run";
          return (
            <tr key={c.testCaseId}>
              <td title={c.question}>{_truncate(c.question, 100)}</td>
              <td><StatusBadge status={status} /></td>
              <td>
                {result === undefined
                  ? "—"
                  : result.hasSources ? "yes" : "no"}
              </td>
              <td>
                {result === undefined
                  ? "—"
                  : result.scopeOk ? "ok" : "issue"}
              </td>
              <td>
                <button
                  type="button"
                  onClick={() => onOpenInManualQuery(c.question)}
                  title="Copy this question into Manual Test Query below"
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

// ---- Helpers -------------------------------------------------------

function StatusBadge({ status }: { status: ImportedTestCaseStatus }) {
  const meta = STATUS_META[status];
  return (
    <span
      className={`badge badge--${meta.tone}`}
      title={meta.tooltip}
    >
      {meta.label}
    </span>
  );
}

const STATUS_META: Record<
  ImportedTestCaseStatus,
  { label: string; tone: "ok" | "warn" | "err" | "neutral"; tooltip: string }
> = {
  not_run: {
    label: "Not run",
    tone: "neutral",
    tooltip: "Imported but not yet executed.",
  },
  answered: {
    label: "Answered",
    tone: "ok",
    tooltip: "Orchestrator produced an answer with sources.",
  },
  no_answer: {
    label: "No answer",
    tone: "warn",
    tooltip: "Orchestrator returned an empty or refusal answer.",
  },
  no_sources: {
    label: "No sources",
    tone: "warn",
    tooltip: "Answered but no citations were attached.",
  },
  scope_error: {
    label: "Scope issue",
    tone: "err",
    tooltip: "Sources came from outside the active run.",
  },
  error: {
    label: "Error",
    tone: "err",
    tooltip: "Query path raised. Inspect the run in Manual Test Query.",
  },
};

function _overallLabel(overall: ImportedTestCaseOverallStatus): string {
  switch (overall) {
    case "good":
      return "Good";
    case "needs_review":
      return "Needs review";
    case "poor":
      return "Poor";
  }
}

function _truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1).trimEnd() + "…";
}

function _errorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return `${fallback} (${err.status}: ${err.message})`;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return fallback;
}
