/**
 * Validation tab — Phase 1.
 *
 * Renders the Knowledge Readiness card (status + summary) and the
 * Manual Query Console below it. Generated test sets, batch runs,
 * and tester verdicts arrive in later phases — not in this tab yet.
 *
 * Tab availability is gated upstream in `ResultsSection` via
 * `summary.availableViews.validation`. When this component renders,
 * we know the run is terminal-success and at least one chunk
 * artifact exists.
 */

import { ManualQueryConsole } from "./ManualQueryConsole";

interface ValidationTabProps {
  runId: string;
}

export function ValidationTab({ runId }: ValidationTabProps) {
  return (
    <div
      className="validation-tab"
      style={{ display: "grid", gap: 16 }}
    >
      <KnowledgeReadinessCard />
      <ManualQueryConsole runId={runId} />
    </div>
  );
}

function KnowledgeReadinessCard() {
  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Knowledge readiness</h3>
          <p className="card__subtitle">
            Validation has not been run as a batch. Use the manual
            query console below to verify the index can answer
            questions from this document. Generated validation sets
            arrive in a later phase.
          </p>
        </div>
        <div>
          <span
            className="validation-status validation-status--unknown"
            aria-label="Validation status: not run"
          >
            Not run
          </span>
        </div>
      </div>
    </div>
  );
}
