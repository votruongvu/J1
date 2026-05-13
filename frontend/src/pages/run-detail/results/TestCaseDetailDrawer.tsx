/**
 * Test-case detail drawer.
 *
 * Shown when the tester clicks View on a generated test case that
 * has not been RUN yet — there is no ``ValidationResult`` to
 * render, but the case itself still has answer-relevant info:
 * question, expected answer, evidence quote, source pointer,
 * scope / type / priority / confidence.
 *
 * Distinct from ``ValidationResultDrawer`` so each surface stays
 * focused: this drawer is read-only and never shows checks /
 * citations / tester verdict (those don't exist until the case
 * runs).
 */

import type { ValidationTestCase } from "@/types/review";

interface TestCaseDetailDrawerProps {
  open: boolean;
  testCase: ValidationTestCase | null;
  onClose: () => void;
}

export function TestCaseDetailDrawer({
  open,
  testCase,
  onClose,
}: TestCaseDetailDrawerProps) {
  if (!open || !testCase) return null;

  return (
    <div
      className="drawer is-open"
      role="dialog"
      aria-label="Test case detail"
    >
      <div className="drawer__head">
        <h3>Test case · {testCase.testCaseId}</h3>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={onClose}
          aria-label="Close drawer"
        >
          Close
        </button>
      </div>
      <div className="drawer__body" style={{ display: "grid", gap: 12 }}>
        <section>
          <h4>Question</h4>
          <p>{testCase.question}</p>
        </section>

        <section>
          <h4>Expected answer</h4>
          {testCase.expectedAnswer ? (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                background: "var(--bg-sunken)",
                padding: 12,
                borderRadius: 6,
              }}
            >
              {testCase.expectedAnswer}
            </pre>
          ) : (
            <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
              (no expected answer captured — the generator didn't surface
              one for this case)
            </p>
          )}
        </section>

        {testCase.reason ? (
          <section>
            <h4>Why this question</h4>
            <p style={{ color: "var(--fg-muted)" }}>{testCase.reason}</p>
          </section>
        ) : null}

        {testCase.evidenceQuote ? (
          <section>
            <h4>Evidence quote</h4>
            <div
              style={{
                background: "var(--bg-sunken)",
                padding: "8px 12px",
                borderRadius: 6,
                borderLeft: "3px solid var(--accent, #5b8def)",
              }}
            >
              <div style={{ color: "var(--fg)" }}>
                “{testCase.evidenceQuote}”
              </div>
              {(testCase.sourceArtifactId || testCase.sourceArtifactType) && (
                <div
                  style={{
                    marginTop: 6,
                    fontSize: 12,
                    color: "var(--fg-muted)",
                    fontFamily: "var(--font-mono)",
                  }}
                >
                  {testCase.sourceArtifactType ? (
                    <span>{testCase.sourceArtifactType}</span>
                  ) : null}
                  {testCase.sourceArtifactId ? (
                    <>
                      {testCase.sourceArtifactType ? " · " : null}
                      <code>{testCase.sourceArtifactId}</code>
                    </>
                  ) : null}
                </div>
              )}
            </div>
          </section>
        ) : null}

        <section>
          <h4>Metadata</h4>
          <ul
            style={{
              display: "grid",
              gap: 4,
              listStyle: "none",
              padding: 0,
              fontSize: 13,
            }}
          >
            <li>
              <strong>Scope:</strong>{" "}
              {testCase.validationScope ?? "generic"}
            </li>
            <li>
              <strong>Type:</strong> {testCase.type}
              {testCase.questionType ? (
                <span style={{ color: "var(--fg-muted)" }}>
                  {" "}
                  · {testCase.questionType.replace(/_/g, " ")}
                </span>
              ) : null}
            </li>
            <li>
              <strong>Priority:</strong> {testCase.priority}
            </li>
            {typeof testCase.confidence === "number" ? (
              <li>
                <strong>Confidence:</strong>{" "}
                {testCase.confidence.toFixed(2)}
              </li>
            ) : null}
            {testCase.generatedFrom ? (
              <li>
                <strong>Generated from:</strong> {testCase.generatedFrom}
              </li>
            ) : null}
            {testCase.expectedEvidence ? (
              <li>
                <strong>Expected evidence:</strong>{" "}
                {testCase.expectedEvidence}
              </li>
            ) : null}
            {testCase.expectedChunks.length ? (
              <li>
                <strong>Expected chunks:</strong>{" "}
                {testCase.expectedChunks.length}
              </li>
            ) : null}
            {testCase.expectedPages.length ? (
              <li>
                <strong>Expected pages:</strong>{" "}
                {testCase.expectedPages.join(", ")}
              </li>
            ) : null}
          </ul>
        </section>

        <section>
          <p
            style={{
              fontSize: 12,
              color: "var(--fg-muted)",
              fontStyle: "italic",
            }}
          >
            This case has not been run yet. Run the validation set to
            see the actual answer, retrieved chunks, and per-check
            outcomes.
          </p>
        </section>
      </div>
    </div>
  );
}
