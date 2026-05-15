/**
 * SearchResultView — renders one ManualTestQueryResponse.
 *
 * Layout:
 *
 *   [Validation banner]                   (passed / warn / err)
 *   <h2>Question</h2>
 *   <p>{question}</p>
 *   <h3>Answer</h3>
 *   <p>{answer}</p>
 *   <h3>Sources</h3>
 *   <ul> ... per citation, with optional Document/Run links</ul>
 *   <details>Show retrieval details</details>   (hidden by default)
 *
 * Pure presentation — owner (GlobalSearchPage) owns the
 * fetch state. The "Show retrieval details" disclosure is a
 * native `<details>` so it stays accessible without extra JS
 * and persists across re-renders.
 */

import { Banner, type BannerKind } from "@/components/Banner";
import type { ManualTestQueryResponse } from "@/types/review";

import {
  retrievalRowsFrom,
  sourceRowsFrom,
  validationStatusKind,
  validationStatusLabel,
} from "./global-search-helpers";


// Map the engine's three-state verdict onto the Banner's three
// kinds. `ok` (passed) renders as `info` so the page doesn't
// scream success — a green banner above the answer would be
// over-affirming when the engine can still be wrong.
function _bannerKindFor(
  status: ManualTestQueryResponse["validationStatus"],
): BannerKind {
  const kind = validationStatusKind(status);
  if (kind === "ok") return "info";
  return kind;
}


interface SearchResultViewProps {
  response: ManualTestQueryResponse;
  /** Open the document detail page for one source row. */
  onOpenDocument: (documentId: string) => void;
  /** Open the run detail page for one source row. */
  onOpenRun: (runId: string) => void;
}


export function SearchResultView({
  response,
  onOpenDocument,
  onOpenRun,
}: SearchResultViewProps) {
  const sources = sourceRowsFrom(response);
  const retrieval = retrievalRowsFrom(response);
  const verdictLabel = validationStatusLabel(response.validationStatus);

  return (
    <div className="search-result" data-testid="search-result">
      <Banner
        kind={_bannerKindFor(response.validationStatus)}
        title={verdictLabel}
      />

      <section className="search-result__question">
        <h2>Question</h2>
        <p data-testid="search-result-question">{response.question}</p>
      </section>

      <section className="search-result__answer card">
        <h3 className="card__title">Answer</h3>
        {response.answer ? (
          <p
            className="search-result__answer-text"
            data-testid="search-result-answer"
          >
            {response.answer}
          </p>
        ) : (
          <p
            className="search-result__answer-empty"
            data-testid="search-result-empty-answer"
          >
            No answer could be synthesised. Inspect retrieval details
            below for evidence the engine saw.
          </p>
        )}
      </section>

      <section className="search-result__sources card">
        <h3 className="card__title">
          Sources <span className="muted">({sources.length})</span>
        </h3>
        {sources.length === 0 ? (
          <p className="muted">No sources were returned with this answer.</p>
        ) : (
          <ul
            className="search-result__source-list"
            data-testid="search-result-sources"
          >
            {sources.map((s) => (
              <li
                key={s.artifactId}
                className="search-result__source"
                data-testid={`search-result-source-${s.artifactId}`}
              >
                <div className="search-result__source-meta">
                  <strong>{s.label}</strong>
                  <span className="muted"> · {s.artifactType}</span>
                </div>
                <div className="search-result__source-actions">
                  {s.sourceDocumentId && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => onOpenDocument(s.sourceDocumentId!)}
                      data-testid={`source-open-document-${s.artifactId}`}
                    >
                      View document
                    </button>
                  )}
                  {s.runId && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => onOpenRun(s.runId!)}
                      data-testid={`source-open-run-${s.artifactId}`}
                    >
                      View run
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {retrieval.length > 0 && (
        <details
          className="search-result__retrieval"
          data-testid="search-result-retrieval-toggle"
        >
          <summary>
            Show retrieval details ({retrieval.length} chunks)
          </summary>
          <ul
            className="search-result__retrieval-list"
            data-testid="search-result-retrieval-list"
          >
            {retrieval.map((row, i) => (
              <li
                key={`${row.artifactId}:${row.chunkId ?? i}`}
                className="search-result__retrieval-row"
              >
                <div className="search-result__retrieval-meta">
                  <strong>{row.artifactKind ?? row.artifactId}</strong>
                  <span className="muted">
                    {" "}· score {row.score.toFixed(3)}
                  </span>
                </div>
                <pre className="search-result__retrieval-preview">
                  {row.preview}
                </pre>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
