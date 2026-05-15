/**
 * Static-markup tests for `SearchResultView`.
 *
 * The component is presentation-only; rendering it through
 * `react-dom/server` is sufficient to pin every branch the
 * operator can see: validation banner, empty answer, empty
 * sources, retrieval-details disclosure.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { SearchResultView } from "../SearchResultView";
import type { ManualTestQueryResponse } from "@/types/review";


function _response(
  over: Partial<ManualTestQueryResponse> = {},
): ManualTestQueryResponse {
  return {
    requestId: "req-1",
    runId: "",
    question: "How often must abutments be inspected?",
    answer: "Every 24 months.",
    modeUsed: "hybrid",
    retrievedChunks: [
      {
        artifactId: "art-a",
        chunkId: "chunk-1",
        runId: "run-1",
        documentId: "doc-1",
        sourceLocation: "Section 3.2",
        score: 0.87,
        preview: "Bridge abutments must be inspected every 24 months.",
        artifactKind: "chunk",
      },
    ],
    citations: [
      {
        artifactId: "art-a",
        artifactType: "chunk",
        sourceDocumentId: "doc-1",
        sourceLocation: "Section 3.2",
        chunkId: "chunk-1",
        runId: "run-1",
      },
    ],
    checks: [],
    validationStatus: "passed",
    evidenceFlags: {
      graphUsed: false,
      tablesUsed: false,
      imagesUsed: false,
    },
    ...over,
  };
}


function _render(response: ManualTestQueryResponse) {
  return renderToStaticMarkup(
    createElement(SearchResultView, {
      response,
      onOpenDocument: () => {},
      onOpenRun: () => {},
    }),
  );
}


describe("SearchResultView", () => {
  it("renders the question + answer + verdict banner", () => {
    const html = _render(_response());
    expect(html).toContain("search-result");
    expect(html).toContain("How often must abutments be inspected?");
    expect(html).toContain("Every 24 months.");
    expect(html).toContain("Answer grounded in cited sources");
  });

  it("renders an empty-answer hint when the engine had no answer", () => {
    const html = _render(_response({ answer: "" }));
    expect(html).toContain("search-result-empty-answer");
    expect(html).toContain("No answer could be synthesised");
  });

  it("renders one row per source with stable testids", () => {
    const html = _render(_response());
    expect(html).toContain("search-result-source-art-a");
    expect(html).toContain("source-open-document-art-a");
    expect(html).toContain("source-open-run-art-a");
    expect(html).toContain("Section 3.2");
  });

  it("renders 'No sources' copy when citations are empty", () => {
    const html = _render(_response({ citations: [] }));
    expect(html).toContain("No sources were returned with this answer");
  });

  it("hides retrieval-details disclosure when no chunks", () => {
    const html = _render(_response({ retrievedChunks: [] }));
    expect(html).not.toContain("search-result-retrieval-toggle");
  });

  it("renders retrieval-details disclosure when chunks present", () => {
    const html = _render(_response());
    expect(html).toContain("search-result-retrieval-toggle");
    expect(html).toContain("Show retrieval details");
    // Open by default? No — `<details>` is closed by default. The
    // operator must click to expand. We render the list HTML
    // regardless so the disclosure is purely a presentation
    // affordance.
    expect(html).toContain("search-result-retrieval-list");
    // Score formatted to 3 decimals.
    expect(html).toContain("score 0.870");
  });

  it("renders the failed-verdict banner kind on a failed result", () => {
    const html = _render(_response({ validationStatus: "failed" }));
    expect(html).toContain("Answer could not be grounded");
    // Failed maps to err banner; pin the class so a future
    // refactor doesn't accidentally swap it for a success tone.
    expect(html).toContain("banner--err");
  });
});
