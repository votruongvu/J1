/**
 * Pure helpers for the Global Search page. Render-friendly
 * projections over the backend's `ManualTestQueryResponse` shape
 * so the page itself stays a thin presentation layer.
 */

import type {
  ManualTestQueryResponse,
  ValidationCitation,
  ValidationRetrievedChunk,
} from "@/types/review";


/**
 * One row in the "Sources" list. Distilled from
 * `response.citations` — the operator-readable evidence the
 * answer relied on. The Global Search page renders one card
 * per source with optional document/run links.
 */
export interface SourceRow {
  artifactId: string;
  artifactType: string;
  /** Operator-readable label — falls back to artifact id when
   * the citation doesn't carry a `sourceLocation`. */
  label: string;
  sourceDocumentId: string | null;
  sourceLocation: string | null;
  runId: string | null;
}


export function sourceRowsFrom(
  response: ManualTestQueryResponse,
): readonly SourceRow[] {
  return (response.citations ?? []).map(
    (c: ValidationCitation): SourceRow => ({
      artifactId: c.artifactId,
      artifactType: c.artifactType,
      label: c.sourceLocation || c.artifactId,
      sourceDocumentId: c.sourceDocumentId ?? null,
      sourceLocation: c.sourceLocation ?? null,
      runId: c.runId ?? null,
    }),
  );
}


/**
 * One row in the "Retrieval details" drawer. Distilled from
 * `response.retrievedChunks` — the raw scored hits BEFORE the
 * answer synthesis. Hidden by default; toggled on by the
 * operator when they want to see how the engine ranked
 * evidence (debug surface, not the daily-use view).
 */
export interface RetrievalRow {
  artifactId: string;
  chunkId: string | null;
  artifactKind: string | null;
  documentId: string | null;
  score: number;
  /** First ~200 chars of the chunk so the operator can sanity-
   * check that the relevant text was retrieved without opening
   * the full artifact. */
  preview: string;
}


export function retrievalRowsFrom(
  response: ManualTestQueryResponse,
): readonly RetrievalRow[] {
  return (response.retrievedChunks ?? []).map(
    (chunk: ValidationRetrievedChunk): RetrievalRow => ({
      artifactId: chunk.artifactId,
      chunkId: chunk.chunkId ?? null,
      artifactKind: chunk.artifactKind ?? null,
      documentId: chunk.documentId ?? null,
      score: chunk.score,
      preview: chunk.preview,
    }),
  );
}


/**
 * Human-readable label for the engine's validation verdict.
 * The Global Search page renders this above the answer so the
 * operator knows whether to trust it.
 */
export function validationStatusLabel(
  status: ManualTestQueryResponse["validationStatus"],
): string {
  switch (status) {
    case "passed":
      return "Answer grounded in cited sources";
    case "passed_with_warnings":
      return "Answer mostly grounded — review warnings";
    case "failed":
      return "Answer could not be grounded";
    case "inconclusive":
      return "Answer is inconclusive";
  }
}


export function validationStatusKind(
  status: ManualTestQueryResponse["validationStatus"],
): "ok" | "warn" | "err" {
  switch (status) {
    case "passed":
      return "ok";
    case "passed_with_warnings":
    case "inconclusive":
      return "warn";
    case "failed":
      return "err";
  }
}
