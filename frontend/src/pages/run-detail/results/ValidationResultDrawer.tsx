/**
 * Validation result detail drawer — shows one test case's outcome.
 *
 * Mirrors the existing `ChunkDrawer` two-view pattern (Readable +
 * Raw JSON) so the FE feels consistent across surfaces.
 *
 * tester verdict UI. The tester can record `pass` /
 * `warning` / `fail` plus free-form notes. The recorded verdict is
 * a SEPARATE signal from the auto `status` — both are surfaced
 * side-by-side; the deterministic outcome stays unchanged.
 */

import { useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { ApiError } from "@/lib/api/client";
import type { ValidationResult } from "@/types/review";

interface ValidationResultDrawerProps {
  open: boolean;
  result: ValidationResult | null;
  /** Run id of the parent ingestion run — needed for verdict POST. */
  runId: string;
  /** Validation run id this result belongs to. */
  validationRunId: string | null;
  /** Called after a successful verdict POST so the parent can
 * refresh its `latestRun` state and re-render the table. */
  onVerdictRecorded?: () => void;
  onClose: () => void;
}

const _VERDICT_OPTIONS: Array<{
  value: "pass" | "warning" | "fail";
  label: string;
  cls: string;
}> = [
  { value: "pass", label: "Pass", cls: "validation-status--ok" },
  { value: "warning", label: "Warning", cls: "validation-status--warn" },
  { value: "fail", label: "Fail", cls: "validation-status--fail" },
];

export function ValidationResultDrawer({
  open,
  result,
  runId,
  validationRunId,
  onVerdictRecorded,
  onClose,
}: ValidationResultDrawerProps) {
  const client = useClient();
  const [showRaw, setShowRaw] = useState(false);
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState<
    "pass" | "warning" | "fail" | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  // Reset local state when the drawer opens for a new result so
  // notes/errors don't leak between cases.
  useEffect(() => {
    if (result) {
      setNotes(result.testerNotes ?? "");
      setError(null);
      setSubmitting(null);
    }
  }, [result?.resultId]);

  if (!open || !result) return null;

  const submitVerdict = async (verdict: "pass" | "warning" | "fail") => {
    if (!validationRunId) return;
    setSubmitting(verdict);
    setError(null);
    try {
      await client.recordTesterVerdict(
        runId,
        validationRunId,
        result.resultId,
        { verdict, notes: notes.trim() || null },
      );
      onVerdictRecorded?.();
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Verdict failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Verdict failed.";
      setError(msg);
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div
      className="drawer is-open"
      role="dialog"
      aria-label="Validation result detail"
    >
      <div className="drawer__head">
        <h3>Result · {result.testCaseId}</h3>
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
          <p>{result.question}</p>
        </section>

        <section>
          <h4>Status</h4>
          <p>
            <strong>Auto: {result.status}</strong>
            {result.testerVerdict && (
              <>
                {" "}· Tester: <strong>{result.testerVerdict}</strong>
              </>
            )}
            {result.failureReason ? <> — {result.failureReason}</> : null}
          </p>
        </section>

        <section>
          <h4>Answer</h4>
          {result.answer ? (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                background: "var(--bg-sunken)",
                padding: 12,
                borderRadius: 6,
              }}
            >
              {result.answer}
            </pre>
          ) : (
            <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
              (no answer)
            </p>
          )}
        </section>

        <section>
          <h4>Checks ({result.checks.length})</h4>
          <ul style={{ display: "grid", gap: 4, listStyle: "none", padding: 0 }}>
            {result.checks.map((c) => (
              <li key={c.name}>
                {c.passed ? "✓" : "✗"} <strong>{c.name}</strong>{" "}
                <small>({c.severity})</small>
                {c.detail ? (
                  <span
                    style={{ display: "block", marginLeft: 24, color: "var(--fg-muted)" }}
                  >
                    {c.detail}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4>Citations ({result.citations.length})</h4>
          <ul style={{ display: "grid", gap: 4, listStyle: "none", padding: 0 }}>
            {result.citations.map((c, i) => (
              <li
                key={`${c.artifactId}-${i}`}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                <code>{c.artifactType}</code> · artifact{" "}
                <code>{c.artifactId}</code>
                {c.chunkId ? <> · chunk <code>{c.chunkId}</code></> : null}
                {c.sourceLocation ? <> · {c.sourceLocation}</> : null}
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4>Retrieved chunks ({result.retrievedChunks.length})</h4>
          <ul style={{ display: "grid", gap: 4, listStyle: "none", padding: 0 }}>
            {result.retrievedChunks.map((c, i) => (
              <li
                key={`${c.artifactId}-${i}`}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {c.chunkId ? <code>{c.chunkId}</code> : <code>{c.artifactId}</code>}
                {c.sourceLocation ? <> · {c.sourceLocation}</> : null}
                {c.preview ? <> — {c.preview}</> : null}
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4>Tester verdict</h4>
          <p style={{ color: "var(--fg-muted)", fontSize: 13 }}>
            Record a human override. The auto status above stays
            unchanged — both signals are kept side-by-side.
          </p>
          <textarea
            rows={3}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional notes (e.g. 'tested manually — answer is correct')"
            style={{ width: "100%", fontFamily: "inherit", padding: 8 }}
            disabled={submitting !== null}
            maxLength={4096}
          />
          <div
            style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}
          >
            {_VERDICT_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                className={`btn validation-status ${opt.cls}`}
                disabled={submitting !== null || !validationRunId}
                onClick={() => void submitVerdict(opt.value)}
              >
                {submitting === opt.value
                  ? "Saving…"
                  : `Mark ${opt.label}`}
              </button>
            ))}
          </div>
          {error && (
            <div
              className="banner banner--err"
              role="alert"
              style={{ marginTop: 8 }}
            >
              {error}
            </div>
          )}
        </section>

        <section>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => setShowRaw((v) => !v)}
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
              {JSON.stringify(result, null, 2)}
            </pre>
          )}
        </section>
      </div>
    </div>
  );
}
