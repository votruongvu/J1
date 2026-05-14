/**
 * Test Active Knowledge — Document Detail-side query widget.
 *
 * Lets a user ask a question against the document's currently
 * active snapshot. Scope is fixed to ``document_active`` (the typed
 * snapshot-centric contract). The widget routes the request through
 * the producing run's manual-test-query endpoint, because that's
 * the only test-query endpoint today — the URL run id is a routing
 * key; the actual query is scoped by the typed ``scope`` field.
 *
 * UX intentionally minimal:
 *   - one question textarea + Run button
 *   - one-line answer + status badge
 *   - a "View full trace" link that deep-links into Run Detail's
 *     Validation tab for the producing run, where the operator can
 *     inspect retrieved chunks, citations, gate results, etc.
 *
 * Why not reuse ManualQueryConsole verbatim? It carries Run Detail
 * affordances (top-k, citation required, synthesize toggle, raw
 * drawer) and a "Scope" dropdown. On Document Detail the scope is
 * fixed — exposing the dropdown would invite operators to pick
 * the wrong knowledge boundary.
 */

import { useCallback, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import { validationStatusMeta } from "@/lib/display";
import type { ManualTestQueryResponse } from "@/types/review";


interface TestActiveKnowledgePanelProps {
  documentId: string;
  /** ID of the document's active snapshot. ``null`` disables the
   *  widget (no active knowledge to test). */
  activeSnapshotId: string | null;
  /** Run that produced the active snapshot. Used only as the URL
   *  routing key for the test-query endpoint; the typed ``scope``
   *  determines what's actually queried. ``null`` disables the
   *  widget (no run to route through). */
  producingRunId: string | null;
  /** Open Run Detail for full trace. */
  onOpenRun: (runId: string) => void;
}


export function TestActiveKnowledgePanel({
  documentId, activeSnapshotId, producingRunId, onOpenRun,
}: TestActiveKnowledgePanelProps) {
  const client = useClient();
  const [question, setQuestion] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [response, setResponse] = useState<ManualTestQueryResponse | null>(null);

  const ready = !!activeSnapshotId && !!producingRunId;

  const submit = useCallback(async () => {
    if (!ready || !producingRunId) return;
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question.");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const result = await client.runManualTestQuery(producingRunId, {
        question: trimmed,
        // Fixed scope: this widget tests the document's active
        // knowledge. Operators who want to inspect a candidate
        // snapshot go to Run Detail instead.
        scope: { type: "document_active", documentId },
        includeRaw: false,
        synthesize: true,
      });
      setResponse(result);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `Query failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Query failed.",
      );
    } finally {
      setRunning(false);
    }
  }, [client, ready, producingRunId, question, documentId]);

  if (!ready) {
    return (
      <p className="muted">
        No active knowledge snapshot to test yet. Re-index this
        document to build the first knowledge version.
      </p>
    );
  }

  return (
    <div className="test-active-knowledge-panel">
      <div className="test-active-knowledge-panel__scope-badge">
        <span>Scope:</span>
        <strong>This document's active snapshot</strong>
        <code className="mono" title={activeSnapshotId ?? undefined}>
          {activeSnapshotId?.slice(0, 12)}…
        </code>
      </div>
      <textarea
        rows={3}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        placeholder="Ask a question to test the active knowledge…"
        disabled={running}
        data-testid="test-active-knowledge-question"
      />
      <div className="test-active-knowledge-panel__actions">
        <button
          type="button"
          className="btn btn--primary btn--sm"
          onClick={() => void submit()}
          disabled={running || !question.trim()}
          data-testid="test-active-knowledge-run"
        >
          {running ? "Running…" : "Run query"}
        </button>
        {response && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => onOpenRun(producingRunId)}
            title="Open the producing run for the full pipeline trace"
          >
            View full trace
          </button>
        )}
      </div>
      {error && (
        <div className="banner banner--err" role="alert">
          {error}
        </div>
      )}
      {response && (
        <TestActiveKnowledgeResult response={response} />
      )}
    </div>
  );
}


function TestActiveKnowledgeResult(
  { response }: { response: ManualTestQueryResponse },
) {
  const meta = validationStatusMeta(response.validationStatus);
  const chunkCount = response.retrievedChunks?.length ?? 0;
  return (
    <div className="test-active-knowledge-panel__result">
      <div className="test-active-knowledge-panel__status">
        <span className={`badge ${meta.className}`}>{meta.label}</span>
        <span className="muted">
          {chunkCount} chunk{chunkCount === 1 ? "" : "s"} retrieved
        </span>
      </div>
      <div className="test-active-knowledge-panel__answer">
        {response.answer || response.synthesizedAnswer || (
          <span className="muted">No answer produced.</span>
        )}
      </div>
    </div>
  );
}
