/**
 * Manual test query console — of the Validation tab.
 *
 * Lets a tester ask a single question against an ingested run and
 * see the answer + retrieved chunks + citations + deterministic
 * check results inline. Synchronous: one HTTP request, blocking
 * until the response lands.
 *
 * IMPORTANT: HTTP success and `validationStatus` are independent.
 * A 200 response can carry `validationStatus="failed"` — the badge
 * reflects whether the answer passed the deterministic checks, not
 * whether the query succeeded transport-side.
 */

import { useCallback, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { ApiError } from "@/lib/api/client";
import { validationStatusMeta } from "@/lib/display";
import type {
  LLMTrace,
  ManualTestQueryResponse,
  ValidationCheck,
} from "@/types/review";

interface ManualQueryConsoleProps {
  runId: string;
}

export function ManualQueryConsole({ runId }: ManualQueryConsoleProps) {
  const client = useClient();
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(10);
  const [citationRequired, setCitationRequired] = useState(false);
  // LLM answer synthesis is on by default — that's the "full RAG"
  // flow tester usually want. Turn off for a fast retrieval-only
  // smoke (skips the LLM entirely so the response lands instantly
  // and isn't blocked on a slow local model).
  const [synthesize, setSynthesize] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [response, setResponse] = useState<ManualTestQueryResponse | null>(
    null,
  );
  const [showRaw, setShowRaw] = useState(false);

  const submit = useCallback(async () => {
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question to run a manual test query.");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const result = await client.runManualTestQuery(runId, {
        question: trimmed,
        topK,
        citationRequired,
        synthesize,
        // Always request raw — the drawer is gated on the toggle so
        // turning it on doesn't require re-querying.
        includeRaw: true,
      });
      setResponse(result);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Test query failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Test query failed.";
      setError(msg);
    } finally {
      setRunning(false);
    }
  }, [client, runId, question, topK, citationRequired, synthesize]);

  return (
    <div className="manual-query-console">
      <div className="card">
        <div className="card__header">
          <div>
            <h3 className="card__title">Manual test query</h3>
            <p className="card__subtitle">
              Ask one question against this run. Retrieval is restricted
              to artifacts produced by this ingestion run.
            </p>
          </div>
        </div>
        <div className="card__body" style={{ display: "grid", gap: 12 }}>
          <label htmlFor="manual-query-question">Question</label>
          <textarea
            id="manual-query-question"
            rows={3}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. What is the proposal due date?"
            style={{ width: "100%", fontFamily: "inherit", padding: 8 }}
            disabled={running}
          />
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <label
              htmlFor="manual-query-topk"
              style={{ display: "flex", alignItems: "center", gap: 8 }}
            >
              <span>topK</span>
              <input
                id="manual-query-topk"
                type="number"
                min={1}
                max={50}
                value={topK}
                onChange={(e) => setTopK(Number(e.target.value) || 10)}
                style={{ width: 64 }}
                disabled={running}
              />
            </label>
            <label
              style={{ display: "flex", alignItems: "center", gap: 8 }}
            >
              <input
                type="checkbox"
                checked={citationRequired}
                onChange={(e) => setCitationRequired(e.target.checked)}
                disabled={running}
              />
              <span>Require citations</span>
            </label>
            <label
              style={{ display: "flex", alignItems: "center", gap: 8 }}
              title="When on, the LLM synthesizes a final answer from the retrieved chunks. Turn off for retrieval-only debug."
            >
              <input
                type="checkbox"
                checked={synthesize}
                onChange={(e) => setSynthesize(e.target.checked)}
                disabled={running}
              />
              <span>Synthesize answer (LLM)</span>
            </label>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={running || !question.trim()}
              className="btn btn--primary"
            >
              {running ? "Running…" : "Run query"}
            </button>
          </div>
          {error && (
            <div className="banner banner--err" role="alert">
              {error}
            </div>
          )}
        </div>
      </div>

      {response && (
        <ResultPanel
          response={response}
          showRaw={showRaw}
          onToggleRaw={() => setShowRaw((v) => !v)}
        />
      )}
    </div>
  );
}

interface ResultPanelProps {
  response: ManualTestQueryResponse;
  showRaw: boolean;
  onToggleRaw: () => void;
}

function ResultPanel({ response, showRaw, onToggleRaw }: ResultPanelProps) {
  const status = response.validationStatus;
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="card__header">
        <div>
          <h3 className="card__title">Result</h3>
          <p className="card__subtitle">
            requestId: <code>{response.requestId}</code>
          </p>
        </div>
        <div>
          <span
            className={`validation-status ${validationStatusMeta(status).className}`}
            aria-label={`Validation status: ${validationStatusMeta(status).label}`}
          >
            {validationStatusMeta(status).label}
          </span>
        </div>
      </div>
      <div className="card__body" style={{ display: "grid", gap: 16 }}>
        <FinalAnswerSection
          synthesized={response.synthesizedAnswer ?? null}
          llm={response.llm ?? null}
        />

        <section>
          <h4>Retrieval preview</h4>
          <p style={{ color: "var(--fg-muted)", marginTop: -4, fontSize: 12 }}>
            Deterministic snippet bundle from the retriever — used to drive
            the checks below. Not the final answer.
          </p>
          {response.answer ? (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                background: "var(--bg-sunken)",
                padding: 12,
                borderRadius: 6,
              }}
            >
              {response.answer}
            </pre>
          ) : (
            <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
              (engine returned no answer)
            </p>
          )}
        </section>

        <section>
          <h4>Checks ({response.checks.length})</h4>
          <ul
            className="validation-checks"
            style={{ display: "grid", gap: 4, listStyle: "none", padding: 0 }}
          >
            {response.checks.map((c) => (
              <CheckRow key={c.name} check={c} />
            ))}
          </ul>
        </section>

        <section>
          <h4>
            Citations ({response.citations.length})
          </h4>
          {response.citations.length === 0 ? (
            <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
              No citations.
            </p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 4 }}>
              {response.citations.map((c, i) => (
                <li
                  key={`${c.artifactId}-${i}`}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  <code>{c.artifactType}</code>{" "}
                  · artifact <code>{c.artifactId}</code>
                  {c.chunkId ? (
                    <>
                      {" "}
                      · chunk <code>{c.chunkId}</code>
                    </>
                  ) : null}
                  {c.sourceLocation ? (
                    <>
                      {" "}
                      · {c.sourceLocation}
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <h4>
            Retrieved chunks ({response.retrievedChunks.length})
          </h4>
          {response.retrievedChunks.length === 0 ? (
            <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
              No chunks retrieved.
            </p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 4 }}>
              {response.retrievedChunks.map((c, i) => (
                <li
                  key={`${c.artifactId}-${i}`}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {c.chunkId ? (
                    <code>{c.chunkId}</code>
                  ) : (
                    <code>{c.artifactId}</code>
                  )}
                  {c.sourceLocation ? <> · {c.sourceLocation}</> : null}
                  {c.preview ? <> — {c.preview}</> : null}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onToggleRaw}
            aria-expanded={showRaw}
          >
            {showRaw ? "Hide raw payload" : "Show raw payload"}
          </button>
          {showRaw && (
            <pre
              style={{
                marginTop: 8,
                background: "var(--bg-sunken)",
                padding: 12,
                borderRadius: 6,
                overflow: "auto",
                maxHeight: 400,
              }}
            >
              {JSON.stringify(response.rawResponse ?? response, null, 2)}
            </pre>
          )}
        </section>
      </div>
    </div>
  );
}

interface FinalAnswerSectionProps {
  synthesized: string | null;
  llm: LLMTrace | null;
}

function FinalAnswerSection({ synthesized, llm }: FinalAnswerSectionProps) {
  // Three render modes:
  //  1. Synthesis ran and produced text  → show the answer.
  //  2. Synthesis was skipped (opt-out)  → tell the user to flip the toggle.
  //  3. Synthesis was attempted but errored (no client, no_evidence,
  //     LLM raised) → show the error so they can diagnose without
  //     digging into the raw payload.
  const hasAnswer = typeof synthesized === "string" && synthesized.length > 0;
  const errorText = llm?.error ?? null;
  const wasCalled = llm?.called === true;

  return (
    <section>
      <h4 style={{ marginBottom: 4 }}>Final Answer</h4>
      <p style={{ color: "var(--fg-muted)", marginTop: 0, fontSize: 12 }}>
        LLM-synthesized answer grounded in the retrieved chunks below.
      </p>
      {hasAnswer ? (
        <pre
          style={{
            whiteSpace: "pre-wrap",
            background: "var(--accent-soft, #eef4ff)",
            padding: 12,
            borderRadius: 6,
            border: "1px solid var(--border)",
            fontSize: 14,
            fontFamily: "inherit",
          }}
        >
          {synthesized}
        </pre>
      ) : !wasCalled ? (
        <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
          {errorText
            ? `LLM disabled: ${errorText}.`
            : "LLM synthesis is off — flip the “Synthesize answer (LLM)” toggle to enable."}
        </p>
      ) : errorText ? (
        <p style={{ color: "var(--err, #b04040)", fontFamily: "var(--font-mono)" }}>
          LLM error: {errorText}
        </p>
      ) : (
        <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
          (no synthesized answer returned)
        </p>
      )}
      {llm && <LLMTraceStrip llm={llm} />}
    </section>
  );
}

function LLMTraceStrip({ llm }: { llm: LLMTrace }) {
  const parts: string[] = [];
  if (llm.provider) parts.push(llm.provider);
  if (llm.model) parts.push(llm.model);
  if (llm.latencyMs != null) parts.push(`${llm.latencyMs} ms`);
  if (llm.promptTokens != null || llm.completionTokens != null) {
    const p = llm.promptTokens ?? 0;
    const c = llm.completionTokens ?? 0;
    parts.push(`${p} in / ${c} out tok`);
  }
  if (!llm.called && parts.length === 0) {
    return null;
  }
  return (
    <p
      style={{
        marginTop: 6,
        fontSize: 11,
        color: "var(--fg-muted)",
        fontFamily: "var(--font-mono)",
      }}
    >
      {llm.called ? "LLM" : "LLM (skipped)"}
      {parts.length > 0 ? ` · ${parts.join(" · ")}` : ""}
    </p>
  );
}

function CheckRow({ check }: { check: ValidationCheck }) {
  const icon = check.passed ? "✓" : "✗";
  const label = check.passed ? "passed" : "failed";
  const cls = check.passed
    ? "validation-check--ok"
    : check.severity === "required"
      ? "validation-check--fail"
      : "validation-check--warn";
  return (
    <li className={`validation-check ${cls}`}>
      <span aria-hidden="true">{icon}</span>{" "}
      <strong>{check.name}</strong>{" "}
      <small>({check.severity}, {label})</small>
      {check.detail ? (
        <span style={{ display: "block", marginLeft: 24, color: "var(--fg-muted)" }}>
          {check.detail}
        </span>
      ) : null}
    </li>
  );
}
