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
  ValidationScope,
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
            {set.domainId ? <> · domain: <code>{set.domainId}</code></> : null}
          </p>
        </div>
      </div>
      {set.llm || set.contextSummary ? (
        <div
          className="card__body"
          style={{
            fontSize: 12,
            color: "var(--fg-muted)",
            borderBottom: "1px solid var(--border)",
            paddingTop: 8,
            paddingBottom: 8,
          }}
        >
          <GeneratorTraceStrip set={set} />
        </div>
      ) : null}
      <div className="card__body" style={{ overflowX: "auto" }}>
        <table
          className="data-table"
          style={{ width: "100%", tableLayout: "fixed" }}
        >
          {/* Fixed widths keep narrow metadata columns from
              sprawling across a wide viewport — without this the
              ~10% per-column default left the table stretched
              with empty whitespace between values. */}
          <colgroup>
            <col />
            <col style={{ width: 120 }} />
            <col style={{ width: 110 }} />
            <col style={{ width: 100 }} />
            <col style={{ width: 160 }} />
            <col style={{ width: 110 }} />
            <col style={{ width: 80 }} />
          </colgroup>
          <thead>
            <tr>
              <th>Question</th>
              <th>Scope</th>
              <th>Type</th>
              <th>Priority</th>
              <th>Evidence</th>
              <th>Last result</th>
              <th style={{ textAlign: "right" }}></th>
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

function GeneratorTraceStrip({ set }: { set: ValidationSet }) {
  const parts: string[] = [];
  if (set.llm?.called) {
    if (set.llm.provider) parts.push(set.llm.provider);
    if (set.llm.model) parts.push(set.llm.model);
    if (set.llm.latencyMs != null) parts.push(`${set.llm.latencyMs} ms`);
    if (set.llm.promptTokens != null || set.llm.completionTokens != null) {
      const p = set.llm.promptTokens ?? 0;
      const c = set.llm.completionTokens ?? 0;
      parts.push(`${p} in / ${c} out tok`);
    }
    if (set.llm.error) parts.push(`error: ${set.llm.error}`);
  } else if (set.llm) {
    parts.push("LLM skipped");
    if (set.llm.error) parts.push(set.llm.error);
  } else {
    parts.push("heuristic (no LLM)");
  }
  const cs = set.contextSummary as
    | { evidenceBlockCount?: number; evidenceCharCount?: number; domainGuidanceUsed?: boolean }
    | undefined;
  const ctxBits: string[] = [];
  if (cs?.evidenceBlockCount != null) {
    ctxBits.push(`${cs.evidenceBlockCount} evidence blocks`);
  }
  if (cs?.evidenceCharCount != null) {
    ctxBits.push(`${cs.evidenceCharCount} chars`);
  }
  if (cs?.domainGuidanceUsed) ctxBits.push("domain rubric");
  return (
    <span style={{ fontFamily: "var(--font-mono)" }}>
      Generator · {parts.join(" · ")}
      {ctxBits.length ? <> · {ctxBits.join(" · ")}</> : null}
    </span>
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
    <>
      <tr className={cls}>
        <td>
          <div>{testCase.question}</div>
          {testCase.expectedAnswer ? (
            <div
              style={{
                marginTop: 4,
                fontSize: 12,
                color: "var(--fg-muted)",
              }}
            >
              <strong>Expected:</strong> {testCase.expectedAnswer}
            </div>
          ) : null}
          {testCase.reason ? (
            <div
              style={{
                marginTop: 4,
                fontSize: 11,
                color: "var(--fg-muted)",
                fontStyle: "italic",
              }}
              title={testCase.reason}
            >
              {testCase.reason}
            </div>
          ) : null}
        </td>
        <td>
          <ScopeBadge scope={testCase.validationScope ?? "generic"} />
          {testCase.generatedFrom ? (
            <div
              style={{
                marginTop: 4,
                fontSize: 10,
                color: "var(--fg-muted)",
                textTransform: "uppercase",
                letterSpacing: 0.04,
              }}
            >
              from {testCase.generatedFrom}
            </div>
          ) : null}
        </td>
        <td>
          <div>{testCase.type}</div>
          {testCase.questionType ? (
            <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
              {testCase.questionType.replace(/_/g, " ")}
            </div>
          ) : null}
        </td>
        <td>
          <div>{testCase.priority}</div>
          {typeof testCase.confidence === "number" ? (
            <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
              conf {testCase.confidence.toFixed(2)}
            </div>
          ) : null}
        </td>
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
        <td style={{ textAlign: "right" }}>
          {/* ``View`` is always available — when a result exists it
              opens the answer/checks/citations drawer; when the
              case hasn't been run yet it opens the test-case
              detail drawer (question + expected answer + evidence
              quote). The tester needs to inspect the generated
              case BEFORE running, not only after. */}
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onSelect}
            aria-label={`View detail for ${testCase.testCaseId}`}
          >
            View
          </button>
        </td>
      </tr>
      {testCase.evidenceQuote ? (
        <tr className={cls}>
          <td colSpan={7} style={{ paddingTop: 0 }}>
            <div
              style={{
                fontSize: 12,
                color: "var(--fg-muted)",
                background: "var(--bg-sunken)",
                padding: "6px 10px",
                borderRadius: 4,
                borderLeft: "3px solid var(--accent, #5b8def)",
              }}
            >
              <strong>Evidence quote</strong>
              {testCase.sourceArtifactId ? (
                <>
                  {" "}
                  · <code style={{ fontSize: 11 }}>{testCase.sourceArtifactId}</code>
                </>
              ) : null}
              {testCase.sourceArtifactType ? (
                <>
                  {" "}
                  ·{" "}
                  <span style={{ fontFamily: "var(--font-mono)" }}>
                    {testCase.sourceArtifactType}
                  </span>
                </>
              ) : null}
              <div style={{ marginTop: 4, color: "var(--fg)" }}>
                “{testCase.evidenceQuote}”
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

const _SCOPE_META: Record<ValidationScope, { label: string; bg: string; fg: string }> = {
  generic: {
    label: "Generic",
    bg: "var(--badge-info-bg, #e6efff)",
    fg: "var(--badge-info-fg, #1f4e9a)",
  },
  document: {
    label: "Document",
    bg: "var(--badge-info-bg, #e6efff)",
    fg: "var(--badge-info-fg, #1f4e9a)",
  },
  domain: {
    label: "Domain",
    bg: "var(--badge-ok-bg, #e6f4ea)",
    fg: "var(--badge-ok-fg, #1e6f35)",
  },
  domain_evidence: {
    label: "Domain-aware",
    bg: "var(--badge-ok-bg, #e6f4ea)",
    fg: "var(--badge-ok-fg, #1e6f35)",
  },
  domain_enrichment: {
    label: "Domain enrichment",
    bg: "var(--badge-accent-bg, #ede5ff)",
    fg: "var(--badge-accent-fg, #4a2db3)",
  },
  graph: {
    label: "Graph",
    bg: "var(--badge-accent-bg, #ede5ff)",
    fg: "var(--badge-accent-fg, #4a2db3)",
  },
  retrieval: {
    label: "Retrieval",
    bg: "var(--badge-info-bg, #e6efff)",
    fg: "var(--badge-info-fg, #1f4e9a)",
  },
  workflow: {
    label: "Workflow",
    bg: "var(--badge-info-bg, #e6efff)",
    fg: "var(--badge-info-fg, #1f4e9a)",
  },
  evidence: {
    label: "Evidence",
    bg: "var(--badge-ok-bg, #e6f4ea)",
    fg: "var(--badge-ok-fg, #1e6f35)",
  },
  guardrail: {
    label: "Guardrail",
    bg: "var(--badge-warn-bg, #fff4d6)",
    fg: "var(--badge-warn-fg, #8a5b00)",
  },
  negative_check: {
    label: "Negative check",
    bg: "var(--badge-warn-bg, #fff4d6)",
    fg: "var(--badge-warn-fg, #8a5b00)",
  },
};

function ScopeBadge({ scope }: { scope: ValidationScope }) {
  const meta = _SCOPE_META[scope] ?? _SCOPE_META.generic;
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 999,
        background: meta.bg,
        color: meta.fg,
        fontSize: 11,
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {meta.label}
    </span>
  );
}

function _evidenceSummary(tc: ValidationTestCase): string {
  // Compact "what does this case expect to find?" hint.
  // ``expectedEvidence`` (post-refactor) is a tester-readable
  // pointer like "page 3, section 'Risk Register'" — use it
  // when present; fall back to the structured chunk/page list.
  if (tc.expectedEvidence) {
    return tc.expectedEvidence;
  }
  const parts: string[] = [];
  if (tc.expectedChunks.length) {
    parts.push(`${tc.expectedChunks.length} chunk${tc.expectedChunks.length === 1 ? "" : "s"}`);
  }
  if (tc.expectedPages.length) {
    parts.push(`pages ${tc.expectedPages.join(", ")}`);
  }
  return parts.length ? parts.join(" · ") : "—";
}
