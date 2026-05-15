/**
 * Pure-logic tests for the Global Search projections.
 *
 * Pin every shape the SearchResultView renders directly off
 * so a regression in the wire decoder is caught at the
 * mapping layer (not visually).
 */

import { describe, expect, it } from "vitest";

import type {
  ManualTestQueryResponse,
  ValidationCitation,
  ValidationRetrievedChunk,
} from "@/types/review";

import {
  retrievalRowsFrom,
  sourceRowsFrom,
  validationStatusKind,
  validationStatusLabel,
} from "../global-search-helpers";


function _citation(over: Partial<ValidationCitation> = {}): ValidationCitation {
  return {
    artifactId: "art-1",
    artifactType: "chunk",
    sourceDocumentId: "doc-1",
    sourceLocation: "Section 3.2",
    chunkId: "chunk-1",
    runId: "run-1",
    ...over,
  };
}


function _chunk(
  over: Partial<ValidationRetrievedChunk> = {},
): ValidationRetrievedChunk {
  return {
    artifactId: "art-1",
    chunkId: "chunk-1",
    runId: "run-1",
    documentId: "doc-1",
    sourceLocation: "Section 3.2",
    score: 0.87,
    preview: "Bridge abutments must be inspected every 24 months.",
    artifactKind: "chunk",
    ...over,
  };
}


function _response(
  over: Partial<ManualTestQueryResponse> = {},
): ManualTestQueryResponse {
  return {
    requestId: "req-1",
    runId: "",
    question: "How often must abutments be inspected?",
    answer: "Every 24 months.",
    modeUsed: "hybrid",
    retrievedChunks: [],
    citations: [],
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


// ---- sourceRowsFrom --------------------------------------------


describe("sourceRowsFrom", () => {
  it("renders each citation as one row", () => {
    const rows = sourceRowsFrom(_response({
      citations: [
        _citation({ artifactId: "a" }),
        _citation({ artifactId: "b" }),
      ],
    }));
    expect(rows.map((r) => r.artifactId)).toEqual(["a", "b"]);
  });

  it("falls back to artifactId as label when sourceLocation missing", () => {
    const rows = sourceRowsFrom(_response({
      citations: [_citation({ sourceLocation: null, artifactId: "art-x" })],
    }));
    expect(rows[0]!.label).toBe("art-x");
  });

  it("uses sourceLocation as label when present", () => {
    const rows = sourceRowsFrom(_response({
      citations: [_citation({ sourceLocation: "Section 3.2" })],
    }));
    expect(rows[0]!.label).toBe("Section 3.2");
  });

  it("returns empty list when no citations", () => {
    expect(sourceRowsFrom(_response()).length).toBe(0);
  });
});


// ---- retrievalRowsFrom -----------------------------------------


describe("retrievalRowsFrom", () => {
  it("renders each retrieved chunk", () => {
    const rows = retrievalRowsFrom(_response({
      retrievedChunks: [
        _chunk({ artifactId: "a", score: 0.9 }),
        _chunk({ artifactId: "b", score: 0.5 }),
      ],
    }));
    expect(rows.map((r) => r.artifactId)).toEqual(["a", "b"]);
    expect(rows[0]!.score).toBe(0.9);
  });

  it("preserves preview text verbatim", () => {
    const rows = retrievalRowsFrom(_response({
      retrievedChunks: [_chunk({ preview: "Tokyo is the capital of Japan." })],
    }));
    expect(rows[0]!.preview).toBe("Tokyo is the capital of Japan.");
  });
});


// ---- validationStatus mappings ---------------------------------


describe("validationStatusLabel / Kind", () => {
  it("maps every validation status to a label + kind", () => {
    expect(validationStatusKind("passed")).toBe("ok");
    expect(validationStatusKind("passed_with_warnings")).toBe("warn");
    expect(validationStatusKind("failed")).toBe("err");
    expect(validationStatusKind("inconclusive")).toBe("warn");

    expect(validationStatusLabel("passed")).toBe(
      "Answer grounded in cited sources",
    );
    expect(validationStatusLabel("failed")).toBe(
      "Answer could not be grounded",
    );
    expect(validationStatusLabel("passed_with_warnings")).toBe(
      "Answer mostly grounded — review warnings",
    );
    expect(validationStatusLabel("inconclusive")).toBe(
      "Answer is inconclusive",
    );
  });
});
