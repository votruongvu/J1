/**
 * KnowledgeMemoryStatusPanel — Phase 3B status surface on the
 * Document Detail page.
 *
 * Compact informational section showing whether the persistent
 * snapshot-scoped `knowledge_memory` artifact has been built
 * (and whether it carries domain insights). Does NOT trigger
 * builds — the existing `ManualActionsPanel` already renders
 * the `Build Knowledge Memory` action button.
 *
 * Data source: `GET /documents/{id}/knowledge-memory` (returns a
 * compact projection over the active snapshot's memory artifact
 * metadata). The endpoint is deployment-gated; on 503 the panel
 * hides itself rather than rendering a confusing "not wired"
 * banner.
 *
 * UX rules:
 *
 *   * The panel NEVER claims the persistent memory artifact
 *     drives query — that's Phase 4 work. Copy uses words like
 *     "prepares" and "available as context for future query
 *     integration."
 *   * Five states map 1:1 to the backend status vocabulary:
 *     `not_built`, `base_compile_only`,
 *     `updated_with_domain_insights`, `failed`, `unknown`.
 *   * `failed` and `unknown` are distinct from `not_built` —
 *     they explain why the operator can't see a happy state.
 *
 * Phase 3B keeps the panel small. The next phase that integrates
 * query with this artifact will extend the copy + add evidence-
 * source breakdown details here.
 */

import { useEffect, useState } from "react";

import type { IngestionClient } from "@/lib/api/client";
import type { KnowledgeMemoryStatusResponse } from "@/types/execution-profile";


export interface KnowledgeMemoryStatusPanelProps {
  client: IngestionClient;
  documentId: string;
  /** When the document has no active snapshot, the panel never
   * fetches and renders the "not built" state with a clear
   * "run an initial ingest first" hint. Optional — pass the
   * value from the Document Detail record. */
  activeSnapshotId: string | null;
}


type LoadState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "loaded"; status: KnowledgeMemoryStatusResponse }
  | { kind: "service_not_wired" }
  | { kind: "error"; message: string };


export function KnowledgeMemoryStatusPanel(
  { client, documentId, activeSnapshotId }: KnowledgeMemoryStatusPanelProps,
) {
  const [state, setState] = useState<LoadState>({ kind: "idle" });

  useEffect(() => {
    let cancelled = false;
    // Skip the fetch when there's no active snapshot — the
    // backend would just return `not_built`, and we already know
    // the answer locally.
    if (!activeSnapshotId) {
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "loading" });
    (async () => {
      try {
        const fetchStatus = (
          client as unknown as {
            getDocumentKnowledgeMemoryStatus?: (
              id: string,
            ) => Promise<KnowledgeMemoryStatusResponse>;
          }
        ).getDocumentKnowledgeMemoryStatus;
        if (typeof fetchStatus !== "function") {
          // Older client / test stub without the method. Treat as
          // "service not wired" — the panel hides.
          if (!cancelled) setState({ kind: "service_not_wired" });
          return;
        }
        const status = await fetchStatus.call(client, documentId);
        if (!cancelled) setState({ kind: "loaded", status });
      } catch (err) {
        if (cancelled) return;
        const message = (
          err as { message?: string } | null
        )?.message ?? "unknown error";
        // 503 fallback: hide the panel rather than render a
        // misleading "build failed" state. Test/minimal deployments
        // don't configure the service; this is the documented
        // graceful-degradation contract.
        if (
          /503/.test(message)
          || /knowledge_memory_service/.test(message)
        ) {
          setState({ kind: "service_not_wired" });
        } else {
          setState({ kind: "error", message });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, documentId, activeSnapshotId]);

  // Pre-snapshot state — render the "not built" hint inline so
  // the section still appears in document detail before the
  // first ingest.
  if (!activeSnapshotId) {
    return (
      <_PanelShell title="Knowledge Memory">
        <p data-testid="kmem-status-not-built">
          <strong>Not built.</strong>{" "}
          The Knowledge Index is built after compile completes.
          Building Knowledge Memory then prepares a query-ready
          projection of compile output and available domain insights.
        </p>
      </_PanelShell>
    );
  }

  if (state.kind === "idle" || state.kind === "loading") {
    return (
      <_PanelShell title="Knowledge Memory">
        <p className="muted" data-testid="kmem-status-loading">
          Loading…
        </p>
      </_PanelShell>
    );
  }

  if (state.kind === "service_not_wired") {
    // Hidden — the deployment doesn't wire the service. Returning
    // null keeps the document detail page clean.
    return null;
  }

  if (state.kind === "error") {
    return (
      <_PanelShell title="Knowledge Memory">
        <p data-testid="kmem-status-error" className="muted">
          Knowledge Memory status unavailable.
        </p>
      </_PanelShell>
    );
  }

  return (
    <_PanelShell title="Knowledge Memory">
      <_StatusBody status={state.status} />
    </_PanelShell>
  );
}


function _PanelShell(
  { title, children }: { title: string; children: React.ReactNode },
) {
  return (
    <section
      className="panel knowledge-memory-status-panel"
      data-testid="knowledge-memory-status-panel"
    >
      <header className="panel__header">
        <h2>{title}</h2>
      </header>
      <div className="panel__body">{children}</div>
    </section>
  );
}


/** Exported for tests: pure render of the per-status body. The
 * panel above wires this into the async fetch path; tests can
 * exercise each status string deterministically without driving
 * effects through a DOM harness. */
export function KnowledgeMemoryStatusBody(
  { status }: { status: KnowledgeMemoryStatusResponse },
) {
  return <_StatusBody status={status} />;
}


function _StatusBody({ status }: { status: KnowledgeMemoryStatusResponse }) {
  switch (status.status) {
    case "not_built":
      return (
        <p data-testid="kmem-status-not-built">
          <strong>Not built.</strong>{" "}
          The Knowledge Index is ready and can be queried. Building
          Knowledge Memory prepares a query-ready projection of
          compile output and available domain insights.
        </p>
      );

    case "base_compile_only":
      return (
        <div data-testid="kmem-status-base">
          <p>
            <strong>Built from base compile.</strong>{" "}
            Query is available using the base Knowledge Index.
            Domain-specific answers may improve after post-compile
            domain enrichment updates Knowledge Memory.
          </p>
          <_MemoryDetails status={status} />
        </div>
      );

    case "updated_with_domain_insights":
      return (
        <div data-testid="kmem-status-updated">
          <p>
            <strong>Updated with domain insights.</strong>{" "}
            Post-compile domain enrichment has been projected into
            Knowledge Memory. Domain-aware answers can use richer
            context such as risks, requirements, validation checks,
            BOQ rows, aliases, and domain summaries.
          </p>
          <_MemoryDetails status={status} />
        </div>
      );

    case "failed":
      return (
        <div data-testid="kmem-status-failed">
          <p>
            <strong>Build failed.</strong>{" "}
            The Knowledge Index is still available for query. You
            can retry building Knowledge Memory.
          </p>
        </div>
      );

    case "unknown":
    default:
      return (
        <p data-testid="kmem-status-unknown" className="muted">
          Knowledge Memory status is currently unknown.
          {status.warnings.length > 0 && (
            <span> ({status.warnings.join(", ")})</span>
          )}
        </p>
      );
  }
}


function _MemoryDetails({ status }: { status: KnowledgeMemoryStatusResponse }) {
  const parts: string[] = [];
  if (status.entryCount > 0) {
    parts.push(
      `${status.entryCount} ${
        status.entryCount === 1 ? "entry" : "entries"
      }`,
    );
  }
  if (status.lastTrigger) {
    parts.push(`built via ${_humanTrigger(status.lastTrigger)}`);
  }
  if (status.lastBuiltAt) {
    parts.push(`at ${status.lastBuiltAt}`);
  }
  if (parts.length === 0) return null;
  return (
    <small
      className="muted knowledge-memory-status-panel__details"
      data-testid="kmem-status-details"
    >
      {parts.join(" · ")}
    </small>
  );
}


function _humanTrigger(trigger: string): string {
  switch (trigger) {
    case "after_compile":
      return "automatic build after compile";
    case "after_domain_enrichment":
      return "automatic rebuild after domain enrichment";
    case "manual":
      return "manual action";
    default:
      return trigger;
  }
}
