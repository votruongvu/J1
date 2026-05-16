/**
 * KnowledgeMemoryQueryDiagnostics — Phase 5C surface.
 *
 * Renders the Knowledge Memory query-side diagnostics that ride
 * on the orchestrator's `QueryTrace.knowledge_memory` block. The
 * block is stamped by:
 *
 *   * Phase 4 — `KnowledgeMemoryContextProvider` (status,
 *     selected entries, expansion terms, warnings).
 *   * Phase 5A — expansion-merge applied state
 *     (`applied_expansion_terms`, truncation flag).
 *   * Phase 5B — source-ref → evidence resolver state
 *     (`injected_evidence_count`, `deduped_evidence_count`,
 *     resolver warnings, `evidence_injection_applied`).
 *
 * The component is presentation-only; it never re-shapes the
 * trace or hides fields the orchestrator emitted. The "Show
 * details" `<details>` toggle keeps the daily-use view compact
 * while still surfacing every diagnostic an operator may need.
 *
 * **UX guardrails (load-bearing)**:
 *
 *   * Memory is a *helper*, never the source of truth. Every
 *     status string + body line is worded to make this clear —
 *     the answer remains grounded in source citations.
 *   * The component never says "the answer came from memory" or
 *     "memory cited X". Memory expands retrieval and points at
 *     source evidence; the existing Sources list is where the
 *     answer's citations live.
 *   * When the orchestrator didn't consult memory at all
 *     (`trace=undefined|null`), the panel renders nothing — no
 *     "Not consulted" stub for legacy / disabled-feature traces
 *     because that would imply we expected memory to fire.
 *
 * Wire-shape note: the trace stamps the provider's
 * `to_payload()` dict verbatim (snake_case). The component keeps
 * snake_case access so an operator can grep the wire dump and
 * see the same field names.
 */

import type { KnowledgeMemoryQueryTrace } from "@/types/review";


// ---- Status vocabulary ----------------------------------------


export const KNOWLEDGE_MEMORY_STATUS_USED = "used";
export const KNOWLEDGE_MEMORY_STATUS_DISABLED = "disabled";
export const KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE = "not_available";
export const KNOWLEDGE_MEMORY_STATUS_LOADED_NO_MATCH = "loaded_no_match";
export const KNOWLEDGE_MEMORY_STATUS_FAILED = "failed";
export const KNOWLEDGE_MEMORY_STATUS_FALLBACK = "fallback";


export interface KnowledgeMemoryStatusView {
  /** Short status chip shown next to the section title. */
  chip: string;
  /** One-sentence body that explains the status. */
  body: string;
  /** Visual tone — drives the chip's CSS class. */
  tone: "ok" | "info" | "warn" | "err";
}


/**
 * Map the wire status string to the operator-visible view. Pure
 * function so tests can drive it independently of React. The
 * fallback case keeps the panel friendly for future status
 * strings the backend may add — we never crash on an unknown
 * status; we render "Used" / "Not available" semantics based on
 * the closest fit and surface the raw string in the details.
 */
export function knowledgeMemoryStatusView(
  trace: KnowledgeMemoryQueryTrace | null | undefined,
): KnowledgeMemoryStatusView {
  const status = (trace?.status ?? "").toLowerCase();
  switch (status) {
    case KNOWLEDGE_MEMORY_STATUS_USED:
      return {
        chip: "Used",
        body: (
          "Knowledge Memory helped expand retrieval and locate "
          + "source evidence. The answer remains grounded in "
          + "source citations."
        ),
        tone: "ok",
      };
    case KNOWLEDGE_MEMORY_STATUS_DISABLED:
      return {
        chip: "Disabled",
        body: "Knowledge Memory query support is disabled.",
        tone: "info",
      };
    case KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE:
      return {
        chip: "Not available",
        body: (
          "No active Knowledge Memory artifact was available for "
          + "this query scope. The query used the standard "
          + "retrieval flow."
        ),
        tone: "info",
      };
    case KNOWLEDGE_MEMORY_STATUS_LOADED_NO_MATCH:
      return {
        chip: "No matching entries",
        body: (
          "Knowledge Memory was available, but no entries matched "
          + "this query."
        ),
        tone: "info",
      };
    case KNOWLEDGE_MEMORY_STATUS_FAILED:
      return {
        chip: "Failed",
        body: (
          "Knowledge Memory lookup failed. J1 continued with the "
          + "standard retrieval flow."
        ),
        tone: "warn",
      };
    case KNOWLEDGE_MEMORY_STATUS_FALLBACK:
      return {
        chip: "Fallback",
        body: (
          "Knowledge Memory triggered a fallback. The query used "
          + "the standard retrieval flow."
        ),
        tone: "warn",
      };
    default:
      return {
        chip: "Not consulted",
        body: "Knowledge Memory was not consulted for this query.",
        tone: "info",
      };
  }
}


// ---- Scope vocabulary ----------------------------------------


function _scopeLabel(scope: string | undefined | null): string {
  if (!scope) return "Document scope";
  if (scope === "project_active") return "Project scope";
  if (scope === "document_active") return "Document scope";
  // Show the raw token for any future scopes — better than
  // silently dropping a value the operator may need.
  return scope;
}


// ---- Summary line --------------------------------------------


/**
 * Build the operator-visible summary line shown directly under
 * the chip. Pulls the most relevant counts off the trace; falls
 * back to an empty string when there's nothing meaningful to
 * summarise.
 */
export function knowledgeMemorySummaryLine(
  trace: KnowledgeMemoryQueryTrace,
): string {
  const parts: string[] = [];
  parts.push(_scopeLabel(trace.scope));

  const artifactCount = trace.memory_artifact_count;
  if (typeof artifactCount === "number" && artifactCount > 0) {
    parts.push(
      `${artifactCount} memory artifact${artifactCount === 1 ? "" : "s"}`,
    );
  }
  const docCount = trace.document_count;
  if (typeof docCount === "number" && docCount > 1) {
    parts.push(`${docCount} documents`);
  }

  const selectedCount = trace.selected_entry_count;
  if (typeof selectedCount === "number" && selectedCount > 0) {
    parts.push(`${selectedCount} matched entries`);
  }

  return parts.join(" · ");
}


// ---- Source-grounding body -----------------------------------


/**
 * Body sentence describing how memory contributed to retrieval +
 * source grounding. Memory NEVER provides the answer; this copy
 * keeps that contract intact.
 */
export function knowledgeMemoryGroundingBody(
  trace: KnowledgeMemoryQueryTrace,
): string | null {
  const appliedTerms = trace.applied_expansion_terms ?? [];
  const injected = trace.injected_evidence_count ?? 0;
  const expansionApplied = (
    trace.expansion_terms_applied ?? false
  ) || appliedTerms.length > 0;
  const evidenceApplied = (
    trace.evidence_injection_applied ?? false
  ) || injected > 0;

  if (!expansionApplied && !evidenceApplied) return null;

  const clauses: string[] = [];
  if (expansionApplied) {
    const n = appliedTerms.length;
    clauses.push(
      `expanded retrieval with ${n} term${n === 1 ? "" : "s"}`,
    );
  }
  if (evidenceApplied) {
    clauses.push(
      `pointed to ${injected} source-grounded evidence `
      + `candidate${injected === 1 ? "" : "s"}`,
    );
  }
  return (
    `Memory ${clauses.join(" and ")}. The answer remains grounded `
    + "in source citations."
  );
}


// ---- Details rows --------------------------------------------


export interface DiagnosticRow {
  key: string;
  label: string;
  value: string;
}


/**
 * Build a deterministic list of (label, value) rows for the
 * "Show details" disclosure. Skips fields that are empty / zero
 * / undefined so the drawer stays compact.
 */
export function knowledgeMemoryDetailRows(
  trace: KnowledgeMemoryQueryTrace,
): DiagnosticRow[] {
  const rows: DiagnosticRow[] = [];
  const pushIfPresent = (
    key: string, label: string, value: unknown,
  ): void => {
    if (value === null || value === undefined) return;
    if (typeof value === "number" && value === 0) return;
    if (Array.isArray(value)) {
      if (value.length === 0) return;
      rows.push({ key, label, value: value.join(", ") });
      return;
    }
    if (typeof value === "boolean") {
      rows.push({ key, label, value: value ? "Yes" : "No" });
      return;
    }
    rows.push({ key, label, value: String(value) });
  };

  pushIfPresent("scope", "Scope", trace.scope);
  pushIfPresent(
    "document_count", "Documents consulted", trace.document_count,
  );
  pushIfPresent(
    "memory_artifact_count",
    "Memory artifacts loaded",
    trace.memory_artifact_count,
  );
  pushIfPresent("entry_count", "Total entries", trace.entry_count);
  pushIfPresent(
    "selected_entry_count",
    "Matched entries",
    trace.selected_entry_count,
  );
  pushIfPresent(
    "selected_entry_types",
    "Matched entry types",
    trace.selected_entry_types,
  );
  pushIfPresent(
    "applied_expansion_terms",
    "Applied expansion terms",
    trace.applied_expansion_terms,
  );
  pushIfPresent(
    "expansion_terms_truncated",
    "Expansion terms truncated",
    trace.expansion_terms_truncated,
  );
  pushIfPresent(
    "resolved_source_ref_count",
    "Resolved source refs",
    trace.resolved_source_ref_count,
  );
  pushIfPresent(
    "injected_evidence_count",
    "Injected source-grounded evidence",
    trace.injected_evidence_count,
  );
  pushIfPresent(
    "deduped_evidence_count",
    "Deduplicated evidence",
    trace.deduped_evidence_count,
  );
  pushIfPresent(
    "unresolved_source_ref_count",
    "Unresolved source refs",
    trace.unresolved_source_ref_count,
  );
  pushIfPresent("artifact_id", "Memory artifact id", trace.artifact_id);
  return rows;
}


/**
 * Combine the Phase-4-base and Phase-5B resolver warnings.
 * Deduplicated for the display, order-stable.
 */
export function knowledgeMemoryWarnings(
  trace: KnowledgeMemoryQueryTrace,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const collect = (arr: string[] | undefined): void => {
    for (const w of arr ?? []) {
      if (!w) continue;
      if (seen.has(w)) continue;
      seen.add(w);
      out.push(w);
    }
  };
  collect(trace.warnings);
  collect(trace.source_ref_resolution_warnings);
  return out;
}


// ---- Component -----------------------------------------------


export interface KnowledgeMemoryQueryDiagnosticsProps {
  /**
   * The trace block as stamped by the orchestrator. ``null`` or
   * ``undefined`` means the orchestrator never consulted memory
   * (legacy / disabled deployments) — the component renders
   * nothing in that case.
   */
  trace: KnowledgeMemoryQueryTrace | null | undefined;
}


export function KnowledgeMemoryQueryDiagnostics(
  { trace }: KnowledgeMemoryQueryDiagnosticsProps,
) {
  // Legacy / disabled-feature traces ship no memory block. Render
  // nothing so the daily-use view doesn't grow a confusing "Not
  // consulted" stub on every query.
  if (!trace) return null;

  const view = knowledgeMemoryStatusView(trace);
  const summary = knowledgeMemorySummaryLine(trace);
  const groundingBody = knowledgeMemoryGroundingBody(trace);
  const rows = knowledgeMemoryDetailRows(trace);
  const warnings = knowledgeMemoryWarnings(trace);

  return (
    <section
      className="knowledge-memory-diagnostics card"
      data-testid="knowledge-memory-diagnostics"
    >
      <header className="knowledge-memory-diagnostics__header">
        <h3 className="card__title">Knowledge Memory</h3>
        <span
          className={
            "knowledge-memory-diagnostics__chip "
            + `knowledge-memory-diagnostics__chip--${view.tone}`
          }
          data-testid="knowledge-memory-diagnostics-chip"
          data-status={trace.status ?? "missing"}
        >
          {view.chip}
        </span>
      </header>

      <p
        className="knowledge-memory-diagnostics__body"
        data-testid="knowledge-memory-diagnostics-body"
      >
        {view.body}
      </p>

      {trace.status === KNOWLEDGE_MEMORY_STATUS_USED && summary && (
        <p
          className="knowledge-memory-diagnostics__summary muted"
          data-testid="knowledge-memory-diagnostics-summary"
        >
          {summary}
        </p>
      )}

      {groundingBody && (
        <p
          className="knowledge-memory-diagnostics__grounding"
          data-testid="knowledge-memory-diagnostics-grounding"
        >
          {groundingBody}
        </p>
      )}

      {warnings.length > 0 && (
        <ul
          className="knowledge-memory-diagnostics__warnings"
          data-testid="knowledge-memory-diagnostics-warnings"
        >
          {warnings.map((w) => (
            <li
              key={w}
              className="knowledge-memory-diagnostics__warning"
              data-testid={`knowledge-memory-diagnostics-warning-${w}`}
            >
              {w}
            </li>
          ))}
        </ul>
      )}

      {rows.length > 0 && (
        <details
          className="knowledge-memory-diagnostics__details"
          data-testid="knowledge-memory-diagnostics-details"
        >
          <summary>Show details</summary>
          <dl className="knowledge-memory-diagnostics__rows">
            {rows.map((row) => (
              <div
                key={row.key}
                className="knowledge-memory-diagnostics__row"
                data-testid={
                  `knowledge-memory-diagnostics-row-${row.key}`
                }
              >
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  );
}


// ---- Extraction helper ---------------------------------------


/**
 * Extract `knowledge_memory` from a `ManualTestQueryResponse`.
 * The orchestrator's trace lands inside `response.debug.
 * orchestrator_trace.knowledge_memory`. The validation service
 * stamps it via `debug["orchestrator_trace"] = result.trace.to_dict()`.
 *
 * Returns ``null`` when the trace / memory block is missing. The
 * shape is intentionally permissive — `debug` is a free-form
 * dict on the wire so we ``unknown``-narrow on read.
 */
export function knowledgeMemoryTraceFrom(
  debug: Record<string, unknown> | undefined | null,
): KnowledgeMemoryQueryTrace | null {
  if (!debug) return null;
  const trace = debug["orchestrator_trace"];
  if (!trace || typeof trace !== "object") return null;
  const km = (trace as Record<string, unknown>)["knowledge_memory"];
  if (!km || typeof km !== "object") return null;
  return km as KnowledgeMemoryQueryTrace;
}
