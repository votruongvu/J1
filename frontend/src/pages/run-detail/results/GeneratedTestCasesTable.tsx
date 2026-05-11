/**
 * Renders the test cases inside a validation set as a table.
 *
 * When the latest validation run is supplied, each row shows the
 * per-case outcome status. Click a row to open the result detail
 * drawer.
 */

import type {
  ValidationResult,
  ValidationResultStatus,
  ValidationRun,
  ValidationSet,
  ValidationTestCase,
} from "@/types/review";

interface GeneratedTestCasesTableProps {
  set: ValidationSet;
  /** Most-recent validation run for this set; null if not yet run. */
  latestRun: ValidationRun | null;
  /** Click handler — passes the testCaseId to the parent. */
  onSelectResult: (testCaseId: string) => void;
}

const _RESULT_LABEL: Record<ValidationResultStatus, string> = {
  passed: "✓ Pass",
  warning: "⚠ Warn",
  failed: "✗ Fail",
  skipped: "⊝ Skip",
};

const _RESULT_CLASS: Record<ValidationResultStatus, string> = {
  passed: "validation-row--ok",
  warning: "validation-row--warn",
  failed: "validation-row--fail",
  skipped: "validation-row--skip",
};

export function GeneratedTestCasesTable({
  set,
  latestRun,
  onSelectResult,
}: GeneratedTestCasesTableProps) {
  // Build a lookup so per-row rendering is O(1). Result is keyed by
  // testCaseId (stable across runs of the same set).
  const resultByCase = new Map<string, ValidationResult>();
  for (const r of latestRun?.results ?? []) {
    resultByCase.set(r.testCaseId, r);
  }

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Generated test cases</h3>
          <p className="card__subtitle">
            {set.testCases.length} cases · source: {set.source} · status:{" "}
            {set.status}
          </p>
        </div>
      </div>
      <div className="card__body" style={{ overflowX: "auto" }}>
        <table className="data-table" style={{ width: "100%" }}>
          <thead>
            <tr>
              <th style={{ width: "40%" }}>Question</th>
              <th>Type</th>
              <th>Priority</th>
              <th>Expected evidence</th>
              <th>Last result</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {set.testCases.map((tc) => (
              <Row
                key={tc.testCaseId}
                testCase={tc}
                result={resultByCase.get(tc.testCaseId)}
                onSelect={() => onSelectResult(tc.testCaseId)}
                hasRun={latestRun !== null}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface RowProps {
  testCase: ValidationTestCase;
  result: ValidationResult | undefined;
  hasRun: boolean;
  onSelect: () => void;
}

function Row({ testCase, result, hasRun, onSelect }: RowProps) {
  const status = result?.status;
  const cls = status ? _RESULT_CLASS[status] : "";
  return (
    <tr className={cls}>
      <td>{testCase.question}</td>
      <td>{testCase.type}</td>
      <td>{testCase.priority}</td>
      <td>{_evidenceSummary(testCase)}</td>
      <td>
        {status ? (
          <span aria-label={`Result: ${status}`}>{_RESULT_LABEL[status]}</span>
        ) : hasRun ? (
          <span style={{ color: "var(--fg-muted)" }}>—</span>
        ) : (
          <span style={{ color: "var(--fg-muted)" }}>not run</span>
        )}
      </td>
      <td>
        {result && (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onSelect}
            aria-label={`View detail for ${testCase.testCaseId}`}
          >
            View
          </button>
        )}
      </td>
    </tr>
  );
}

function _evidenceSummary(tc: ValidationTestCase): string {
  // Compact "what does this case expect to find?" hint. 
  // will add table/image/graph variants; emits chunks +
  // pages.
  const parts: string[] = [];
  if (tc.expectedChunks.length) {
    parts.push(`${tc.expectedChunks.length} chunk${tc.expectedChunks.length === 1 ? "" : "s"}`);
  }
  if (tc.expectedPages.length) {
    parts.push(`pages ${tc.expectedPages.join(", ")}`);
  }
  return parts.length ? parts.join(" · ") : "—";
}
