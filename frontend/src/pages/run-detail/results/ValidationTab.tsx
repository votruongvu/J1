/**
 * Validation tab — Phase 2.
 *
 * Composes the Knowledge Readiness card, Generated Test Cases
 * table, manual query console, and the result-detail drawer.
 *
 * Lifecycle:
 *   - On mount: fetch the latest validation set + latest run.
 *   - Generate Set → POST /validation-sets/generate; refresh.
 *   - Run Validation → POST /validation-runs; refresh; show
 *     summary on the card.
 *   - Click row → fetch full ValidationRun detail and open drawer
 *     scrolled to that result.
 *
 * The split between executionStatus (job state) and validationStatus
 * (test outcome) is honoured throughout — the readiness card shows
 * both, and the table colours each row by its per-case status only.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { ApiError } from "@/lib/api/client";
import type {
  ValidationRun,
  ValidationRunListItem,
  ValidationSet,
  ValidationSetListItem,
} from "@/types/review";

import { GeneratedTestCasesTable } from "./GeneratedTestCasesTable";
import { KnowledgeReadinessCard } from "./KnowledgeReadinessCard";
import { ManualQueryConsole } from "./ManualQueryConsole";
import { ValidationResultDrawer } from "./ValidationResultDrawer";

interface ValidationTabProps {
  runId: string;
}

export function ValidationTab({ runId }: ValidationTabProps) {
  const client = useClient();
  // Latest set in the project for THIS run. Phase 2 ships only one
  // active set per run; if multiple ever exist, we show the most
  // recent (server returns newest-first).
  const [setItem, setSetItem] = useState<ValidationSetListItem | null>(null);
  const [setDetail, setSetDetail] = useState<ValidationSet | null>(null);
  // Latest validation run, surfaced as the readiness card's status.
  const [latestRunItem, setLatestRunItem] =
    useState<ValidationRunListItem | null>(null);
  // Eagerly-fetched detail of latest run — needed to render
  // per-case statuses on the table without a per-row round-trip.
  const [latestRun, setLatestRun] = useState<ValidationRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Drawer state for inspecting a single result.
  const [drawerResultId, setDrawerResultId] = useState<string | null>(null);

  // Refresh: fetch latest set + run together so the page reflects
  // the latest server state. Called after every mutation.
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [setItems, runItems] = await Promise.all([
        client.listValidationSets(runId),
        client.listValidationRuns(runId),
      ]);
      const latestSet = setItems[0] ?? null;
      const latestRunRef = runItems[0] ?? null;
      setSetItem(latestSet);
      setLatestRunItem(latestRunRef);

      // Fetch full detail for both so the FE can render the table
      // + readiness card without secondary calls. Cheap — one set
      // per run + one terminal run snapshot.
      const [detail, runDetail] = await Promise.all([
        latestSet
          ? client.getValidationSet(runId, latestSet.validationSetId)
          : Promise.resolve(null),
        latestRunRef
          ? client.getValidationRun(runId, latestRunRef.validationRunId)
          : Promise.resolve(null),
      ]);
      setSetDetail(detail);
      setLatestRun(runDetail);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Failed to load (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Failed to load validation state.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [client, runId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onGenerate = useCallback(async () => {
    setGenerating(true);
    setError(null);
    try {
      // Force=true on the click so testers can regenerate at will.
      // Idempotency caching is for the auto-on-mount case only.
      await client.generateValidationSet(runId, { force: true });
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `Generate failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Generate failed.",
      );
    } finally {
      setGenerating(false);
    }
  }, [client, runId, refresh]);

  const onRun = useCallback(async () => {
    if (!setItem) {
      setError("No validation set yet. Generate one first.");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      await client.runValidation(runId, {
        validationSetId: setItem.validationSetId,
      });
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `Run failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Run failed.",
      );
    } finally {
      setRunning(false);
    }
  }, [client, runId, setItem, refresh]);

  const onSelectResult = (testCaseId: string) => {
    if (!latestRun) return;
    const result = latestRun.results.find((r) => r.testCaseId === testCaseId);
    if (result) setDrawerResultId(result.resultId);
  };

  const drawerResult = drawerResultId
    ? latestRun?.results.find((r) => r.resultId === drawerResultId) ?? null
    : null;

  return (
    <div className="validation-tab" style={{ display: "grid", gap: 16 }}>
      <KnowledgeReadinessCard
        latestRun={latestRun}
        setItem={setItem}
        running={running}
        generating={generating}
        onGenerate={() => void onGenerate()}
        onRun={() => void onRun()}
        runId={runId}
      />

      {error && (
        <div className="banner banner--err" role="alert">
          {error}
        </div>
      )}

      {loading && !setDetail ? (
        <div className="card">
          <div className="card__body">Loading validation state…</div>
        </div>
      ) : setDetail ? (
        <GeneratedTestCasesTable
          set={setDetail}
          latestRun={latestRun}
          onSelectResult={onSelectResult}
        />
      ) : (
        <div className="card">
          <div className="card__body">
            No validation set yet. Click <strong>Generate Test Set</strong>{" "}
            above to create one from this run's chunks.
          </div>
        </div>
      )}

      <ManualQueryConsole runId={runId} />

      <ValidationResultDrawer
        open={drawerResultId !== null}
        result={drawerResult}
        runId={runId}
        validationRunId={latestRun?.validationRunId ?? null}
        onClose={() => setDrawerResultId(null)}
        // Refresh latestRun (and the table) after a verdict is
        // recorded so the per-row badge re-renders without the
        // tester having to navigate away. Phase 5 contract: the
        // verdict POST returns the updated snapshot, so a single
        // refetch is enough.
        onVerdictRecorded={() => void refresh()}
      />
    </div>
  );
}
