/**
 * Contract — Phase 5C: KnowledgeMemoryQueryDiagnostics surface.
 *
 * Pins:
 *
 *   * No trace block on the response → component renders nothing.
 *   * `status=used` → "Used" chip, expansion-and-evidence summary,
 *     details disclosure, source-grounding sentence.
 *   * `status=disabled` → "Disabled" chip with the right body.
 *   * `status=not_available` → "Not available" chip with the
 *     right body.
 *   * `status=loaded_no_match` → "No matching entries" chip.
 *   * `status=failed` → "Failed" chip + fallback body.
 *   * Unknown / missing status → "Not consulted" fallback.
 *   * Project scope summary includes document + artifact counts.
 *   * Source-grounding counts (resolved / injected / deduped /
 *     unresolved) appear in the details disclosure.
 *   * Warnings + source_ref_resolution_warnings render as a
 *     deduplicated list.
 *   * Copy NEVER claims the answer "came from memory" or that
 *     memory is "source of truth" / "the answer source".
 *   * `knowledgeMemoryTraceFrom` extraction helper handles
 *     missing / malformed debug shapes without throwing.
 *
 * Uses `renderToStaticMarkup` only — same pattern as
 * `SearchResultView.test.ts`. No DOM dependency.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import {
  KNOWLEDGE_MEMORY_STATUS_DISABLED,
  KNOWLEDGE_MEMORY_STATUS_FAILED,
  KNOWLEDGE_MEMORY_STATUS_LOADED_NO_MATCH,
  KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE,
  KNOWLEDGE_MEMORY_STATUS_USED,
  KnowledgeMemoryQueryDiagnostics,
  knowledgeMemoryDetailRows,
  knowledgeMemoryGroundingBody,
  knowledgeMemoryStatusView,
  knowledgeMemorySummaryLine,
  knowledgeMemoryTraceFrom,
  knowledgeMemoryWarnings,
} from "../KnowledgeMemoryQueryDiagnostics";
import type { KnowledgeMemoryQueryTrace } from "@/types/review";


// ---- Helpers ----------------------------------------------------


function _render(
  trace: KnowledgeMemoryQueryTrace | null | undefined,
): string {
  return renderToStaticMarkup(
    createElement(KnowledgeMemoryQueryDiagnostics, { trace }),
  );
}


function _usedTrace(
  over: Partial<KnowledgeMemoryQueryTrace> = {},
): KnowledgeMemoryQueryTrace {
  return {
    status: KNOWLEDGE_MEMORY_STATUS_USED,
    available: true,
    scope: "project_active",
    artifact_id: null,
    document_count: 3,
    memory_artifact_count: 2,
    entry_count: 42,
    selected_entry_count: 4,
    selected_entry_types: ["risk", "requirement"],
    expansion_terms: ["NCR", "non-conformance report"],
    applied_expansion_terms: ["NCR", "non-conformance report"],
    expansion_terms_applied: true,
    expansion_terms_truncated: false,
    resolved_source_ref_count: 5,
    injected_evidence_count: 3,
    deduped_evidence_count: 1,
    unresolved_source_ref_count: 1,
    source_ref_resolution_warnings: [],
    evidence_injection_applied: true,
    warnings: [],
    ...over,
  };
}


// ---- Status view ------------------------------------------------


describe("knowledgeMemoryStatusView", () => {
  it("returns 'Used' for status=used", () => {
    const view = knowledgeMemoryStatusView(_usedTrace());
    expect(view.chip).toBe("Used");
    expect(view.tone).toBe("ok");
    expect(view.body).toContain("source citations");
  });

  it("returns 'Disabled' for status=disabled", () => {
    const view = knowledgeMemoryStatusView({
      status: KNOWLEDGE_MEMORY_STATUS_DISABLED,
    });
    expect(view.chip).toBe("Disabled");
    expect(view.tone).toBe("info");
  });

  it("returns 'Not available' for status=not_available", () => {
    const view = knowledgeMemoryStatusView({
      status: KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE,
    });
    expect(view.chip).toBe("Not available");
    expect(view.body).toContain("standard retrieval flow");
  });

  it("returns 'No matching entries' for status=loaded_no_match", () => {
    const view = knowledgeMemoryStatusView({
      status: KNOWLEDGE_MEMORY_STATUS_LOADED_NO_MATCH,
    });
    expect(view.chip).toBe("No matching entries");
  });

  it("returns 'Failed' for status=failed", () => {
    const view = knowledgeMemoryStatusView({
      status: KNOWLEDGE_MEMORY_STATUS_FAILED,
    });
    expect(view.chip).toBe("Failed");
    expect(view.tone).toBe("warn");
    expect(view.body).toContain("standard retrieval flow");
  });

  it("returns 'Not consulted' for unknown / missing status", () => {
    expect(knowledgeMemoryStatusView({}).chip).toBe("Not consulted");
    expect(knowledgeMemoryStatusView({ status: "totally-new-thing" }).chip)
      .toBe("Not consulted");
  });
});


// ---- Summary line ----------------------------------------------


describe("knowledgeMemorySummaryLine", () => {
  it("renders project scope with artifact + document counts", () => {
    const line = knowledgeMemorySummaryLine(_usedTrace());
    expect(line).toContain("Project scope");
    expect(line).toContain("2 memory artifacts");
    expect(line).toContain("3 documents");
    expect(line).toContain("4 matched entries");
  });

  it("renders document scope when scope=document_active", () => {
    const line = knowledgeMemorySummaryLine(_usedTrace({
      scope: "document_active",
      document_count: 1,
      memory_artifact_count: 1,
    }));
    expect(line).toContain("Document scope");
    expect(line).not.toContain("documents");
  });

  it("uses singular 'memory artifact' when count is 1", () => {
    const line = knowledgeMemorySummaryLine(_usedTrace({
      memory_artifact_count: 1,
    }));
    expect(line).toContain("1 memory artifact");
    expect(line).not.toContain("memory artifacts");
  });
});


// ---- Grounding body --------------------------------------------


describe("knowledgeMemoryGroundingBody", () => {
  it("explains expansion + injection without claiming memory is the answer", () => {
    const body = knowledgeMemoryGroundingBody(_usedTrace());
    expect(body).toBeTruthy();
    expect(body!).toContain("expanded retrieval");
    expect(body!).toContain("source-grounded evidence");
    expect(body!).toContain("source citations");
    // Forbidden copy:
    expect(body!.toLowerCase()).not.toContain("answer came from memory");
    expect(body!.toLowerCase()).not.toContain("memory is the source");
    expect(body!.toLowerCase()).not.toContain("source of truth");
  });

  it("returns null when nothing applied", () => {
    const body = knowledgeMemoryGroundingBody(_usedTrace({
      expansion_terms_applied: false,
      applied_expansion_terms: [],
      evidence_injection_applied: false,
      injected_evidence_count: 0,
    }));
    expect(body).toBeNull();
  });

  it("renders only the expansion clause when no evidence injected", () => {
    const body = knowledgeMemoryGroundingBody(_usedTrace({
      evidence_injection_applied: false,
      injected_evidence_count: 0,
    }));
    expect(body).toContain("expanded retrieval");
    expect(body).not.toContain("source-grounded evidence");
  });

  it("renders only the injection clause when no expansion applied", () => {
    const body = knowledgeMemoryGroundingBody(_usedTrace({
      expansion_terms_applied: false,
      applied_expansion_terms: [],
    }));
    expect(body).not.toContain("expanded retrieval");
    expect(body).toContain("source-grounded evidence");
  });
});


// ---- Detail rows -----------------------------------------------


describe("knowledgeMemoryDetailRows", () => {
  it("emits a row per non-zero diagnostic", () => {
    const rows = knowledgeMemoryDetailRows(_usedTrace());
    const keys = rows.map((r) => r.key);
    expect(keys).toContain("scope");
    expect(keys).toContain("memory_artifact_count");
    expect(keys).toContain("selected_entry_types");
    expect(keys).toContain("applied_expansion_terms");
    expect(keys).toContain("resolved_source_ref_count");
    expect(keys).toContain("injected_evidence_count");
    expect(keys).toContain("deduped_evidence_count");
    expect(keys).toContain("unresolved_source_ref_count");
  });

  it("skips zero / empty fields", () => {
    const rows = knowledgeMemoryDetailRows({
      status: "used",
      memory_artifact_count: 0,
      selected_entry_types: [],
      applied_expansion_terms: [],
      resolved_source_ref_count: 0,
    });
    expect(rows).toHaveLength(0);
  });

  it("renders selected entry types as a comma list", () => {
    const rows = knowledgeMemoryDetailRows(_usedTrace());
    const row = rows.find((r) => r.key === "selected_entry_types");
    expect(row?.value).toBe("risk, requirement");
  });

  it("renders boolean flags as Yes/No", () => {
    const rows = knowledgeMemoryDetailRows(_usedTrace({
      expansion_terms_truncated: true,
    }));
    const row = rows.find((r) => r.key === "expansion_terms_truncated");
    expect(row?.value).toBe("Yes");
  });
});


// ---- Warnings --------------------------------------------------


describe("knowledgeMemoryWarnings", () => {
  it("combines warnings + source_ref_resolution_warnings", () => {
    const w = knowledgeMemoryWarnings({
      warnings: ["memory_artifact_has_no_entries"],
      source_ref_resolution_warnings: ["source_ref_artifact_not_found"],
    });
    expect(w).toContain("memory_artifact_has_no_entries");
    expect(w).toContain("source_ref_artifact_not_found");
  });

  it("deduplicates repeated warnings across both lists", () => {
    const w = knowledgeMemoryWarnings({
      warnings: ["source_ref_cap_applied"],
      source_ref_resolution_warnings: ["source_ref_cap_applied"],
    });
    expect(w).toEqual(["source_ref_cap_applied"]);
  });
});


// ---- Component rendering --------------------------------------


describe("KnowledgeMemoryQueryDiagnostics", () => {
  it("renders nothing when trace is null", () => {
    expect(_render(null)).toBe("");
  });

  it("renders nothing when trace is undefined", () => {
    expect(_render(undefined)).toBe("");
  });

  it("renders the 'Used' chip + summary + grounding sentence", () => {
    const html = _render(_usedTrace());
    expect(html).toContain("knowledge-memory-diagnostics");
    expect(html).toContain("Used");
    expect(html).toContain("Project scope");
    expect(html).toContain("expanded retrieval");
    expect(html).toContain("source-grounded evidence");
    expect(html).toContain("source citations");
  });

  it("never uses misleading copy claiming memory is the source", () => {
    const html = _render(_usedTrace());
    const lower = html.toLowerCase();
    expect(lower).not.toContain("answer came from memory");
    expect(lower).not.toContain("memory answered");
    expect(lower).not.toContain("memory is the source");
    expect(lower).not.toContain("source of truth");
    expect(lower).not.toContain("memory overrides");
  });

  it("renders 'Disabled' state", () => {
    const html = _render({ status: KNOWLEDGE_MEMORY_STATUS_DISABLED });
    expect(html).toContain("Disabled");
    expect(html).toContain("disabled");
  });

  it("renders 'Not available' state with standard-flow body", () => {
    const html = _render({
      status: KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE,
    });
    expect(html).toContain("Not available");
    expect(html).toContain("standard retrieval flow");
  });

  it("renders 'No matching entries' state", () => {
    const html = _render({
      status: KNOWLEDGE_MEMORY_STATUS_LOADED_NO_MATCH,
    });
    expect(html).toContain("No matching entries");
  });

  it("renders 'Failed' state with fallback body", () => {
    const html = _render({ status: KNOWLEDGE_MEMORY_STATUS_FAILED });
    expect(html).toContain("Failed");
    expect(html).toContain("standard retrieval flow");
  });

  it("renders project scope summary with document + artifact counts", () => {
    const html = _render(_usedTrace());
    expect(html).toContain("Project scope");
    expect(html).toContain("2 memory artifacts");
    expect(html).toContain("3 documents");
  });

  it("renders source-grounding counts in the details disclosure", () => {
    const html = _render(_usedTrace());
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-resolved_source_ref_count",
    );
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-injected_evidence_count",
    );
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-deduped_evidence_count",
    );
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-unresolved_source_ref_count",
    );
  });

  it("renders warnings and resolver warnings as a deduped list", () => {
    const html = _render(_usedTrace({
      warnings: ["memory_artifact_has_no_entries"],
      source_ref_resolution_warnings: ["source_ref_artifact_not_found"],
    }));
    expect(html).toContain(
      "knowledge-memory-diagnostics-warning-memory_artifact_has_no_entries",
    );
    expect(html).toContain(
      "knowledge-memory-diagnostics-warning-source_ref_artifact_not_found",
    );
  });

  it("renders applied expansion terms in the details", () => {
    const html = _render(_usedTrace());
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-applied_expansion_terms",
    );
    expect(html).toContain("NCR");
    expect(html).toContain("non-conformance report");
  });

  it("renders selected entry types in the details", () => {
    const html = _render(_usedTrace());
    expect(html).toContain(
      "knowledge-memory-diagnostics-row-selected_entry_types",
    );
    expect(html).toContain("risk, requirement");
  });

  it("does NOT render the summary line when status != used", () => {
    const html = _render({
      status: KNOWLEDGE_MEMORY_STATUS_NOT_AVAILABLE,
      scope: "project_active",
      memory_artifact_count: 0,
    });
    expect(html).not.toContain(
      "knowledge-memory-diagnostics-summary",
    );
  });
});


// ---- Extraction helper ----------------------------------------


describe("knowledgeMemoryTraceFrom", () => {
  it("returns null when debug is undefined", () => {
    expect(knowledgeMemoryTraceFrom(undefined)).toBeNull();
  });

  it("returns null when debug is null", () => {
    expect(knowledgeMemoryTraceFrom(null)).toBeNull();
  });

  it("returns null when orchestrator_trace is missing", () => {
    expect(knowledgeMemoryTraceFrom({})).toBeNull();
  });

  it("returns null when orchestrator_trace.knowledge_memory is missing", () => {
    expect(knowledgeMemoryTraceFrom({
      orchestrator_trace: { final_status: "passed" },
    })).toBeNull();
  });

  it("returns the knowledge_memory block when present", () => {
    const result = knowledgeMemoryTraceFrom({
      orchestrator_trace: {
        knowledge_memory: {
          status: "used",
          available: true,
          scope: "project_active",
        },
      },
    });
    expect(result?.status).toBe("used");
    expect(result?.scope).toBe("project_active");
  });

  it("handles malformed orchestrator_trace gracefully", () => {
    expect(knowledgeMemoryTraceFrom({
      orchestrator_trace: "not-a-dict",
    })).toBeNull();
    expect(knowledgeMemoryTraceFrom({
      orchestrator_trace: { knowledge_memory: "not-a-dict" },
    })).toBeNull();
  });
});


// ---- SearchResultView integration regression -------------------


describe("SearchResultView with knowledge_memory in debug", () => {
  it("renders the diagnostics block when the response carries a memory trace", async () => {
    const { SearchResultView } = await import("../SearchResultView");
    // Build a minimal response with the trace embedded in debug.
    const response = {
      requestId: "req-1",
      runId: "",
      question: "What are the risks?",
      answer: "Several risks were found.",
      modeUsed: "smart_query_orchestrator",
      retrievedChunks: [],
      citations: [],
      checks: [],
      validationStatus: "passed" as const,
      evidenceFlags: {
        graphUsed: false, tablesUsed: false, imagesUsed: false,
      },
      debug: {
        orchestrator_trace: {
          knowledge_memory: {
            status: "used",
            scope: "project_active",
            memory_artifact_count: 2,
            applied_expansion_terms: ["NCR"],
            injected_evidence_count: 1,
            evidence_injection_applied: true,
          },
        },
      } as unknown as never,
    };
    const html = renderToStaticMarkup(
      createElement(SearchResultView, {
        response,
        onOpenDocument: () => {},
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("knowledge-memory-diagnostics");
    expect(html).toContain("Used");
  });

  it("renders no diagnostics block when the response has no debug", async () => {
    const { SearchResultView } = await import("../SearchResultView");
    const response = {
      requestId: "req-1",
      runId: "",
      question: "q",
      answer: "a",
      modeUsed: "smart",
      retrievedChunks: [],
      citations: [],
      checks: [],
      validationStatus: "passed" as const,
      evidenceFlags: {
        graphUsed: false, tablesUsed: false, imagesUsed: false,
      },
    };
    const html = renderToStaticMarkup(
      createElement(SearchResultView, {
        response,
        onOpenDocument: () => {},
        onOpenRun: () => {},
      }),
    );
    expect(html).not.toContain("knowledge-memory-diagnostics");
  });
});
