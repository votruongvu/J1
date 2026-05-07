/**
 * Validation result detail drawer — shows one test case's outcome.
 *
 * Mirrors the existing `ChunkDrawer` two-view pattern (Readable +
 * Raw JSON) so the FE feels consistent across surfaces.
 */

import { useState } from "react";
import type { ValidationResult } from "@/types/review";

interface ValidationResultDrawerProps {
  open: boolean;
  result: ValidationResult | null;
  onClose: () => void;
}

export function ValidationResultDrawer({
  open,
  result,
  onClose,
}: ValidationResultDrawerProps) {
  const [showRaw, setShowRaw] = useState(false);

  if (!open || !result) return null;
  return (
    <div
      className="drawer drawer--open"
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
            <strong>{result.status}</strong>
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
