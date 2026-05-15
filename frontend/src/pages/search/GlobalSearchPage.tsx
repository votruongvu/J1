/**
 * GlobalSearchPage — the dedicated search surface. Owns the
 * fetch state for `client.runProjectQuery` and renders one of
 * four states:
 *
 *   loading  — spinner while the query is in flight
 *   ready    — SearchResultView with the answer + sources
 *   error    — banner with the upstream error message
 *   empty    — landing-state hint when no query has been run
 *
 * Default scope is `project_active` — every attached document's
 * active snapshot. The user can re-execute by editing the input
 * and clicking Search again. No retry-on-error button — the
 * Search action is the retry.
 */

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { Banner } from "@/components/Banner";
import { Icon } from "@/components/icons";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ManualTestQueryRequest,
  ManualTestQueryResponse,
} from "@/types/review";
import type { ProjectContext } from "@/types/ui";

import { SearchResultView } from "./SearchResultView";


interface GlobalSearchPageProps {
  ctx: ProjectContext;
  /** Optional initial query passed in by the Home Dashboard's
   * search card. When set, the page pre-fills the input AND
   * fires the query on mount. */
  initialQuery?: string;
  onBack: () => void;
  onOpenDocument: (documentId: string) => void;
  onOpenRun: (runId: string) => void;
}


type ResultState =
  | { kind: "empty" }
  | { kind: "loading" }
  | { kind: "ready"; response: ManualTestQueryResponse }
  | { kind: "error"; message: string };


export function GlobalSearchPage({
  ctx,
  initialQuery,
  onBack,
  onOpenDocument,
  onOpenRun,
}: GlobalSearchPageProps) {
  const client = useClient();
  const [query, setQuery] = useState(initialQuery ?? "");
  const [state, setState] = useState<ResultState>({ kind: "empty" });

  const ready = !!ctx.tenant && !!ctx.project;

  const execute = useCallback(
    (q: string) => {
      const trimmed = q.trim();
      if (!trimmed || !ready) return;
      setState({ kind: "loading" });
      void (async () => {
        try {
          const request: ManualTestQueryRequest = {
            question: trimmed,
            // `project_active` is the default scope: every
            // attached document's active snapshot. Spelled out
            // here so future scope additions don't silently
            // change the search semantics.
            scope: { type: "project_active" },
            synthesize: true,
          };
          const response = await client.runProjectQuery(request);
          setState({ kind: "ready", response });
        } catch (e) {
          setState({
            kind: "error",
            message: e instanceof Error ? e.message : "Search failed.",
          });
        }
      })();
    },
    [client, ready],
  );

  // Auto-execute if the Home page handed in a query. Runs once
  // on mount via the eslint-recommended pattern of consuming
  // `initialQuery` from a stable prop. `execute` is memoized so
  // the effect doesn't loop.
  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      execute(initialQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    execute(query);
  };

  return (
    <div className="global-search-page" data-testid="global-search-page">
      <div className="page-header">
        <div>
          <a
            href="#"
            onClick={(e) => { e.preventDefault(); onBack(); }}
            className="back-link"
            data-testid="search-back"
          >
            <Icon.ChevronLeft className="icon-sm" /> Home
          </a>
          <h1>Search</h1>
          <p>Ask across all active indexed knowledge.</p>
        </div>
      </div>

      {!ready && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="warn" title="Tenant and Project are required">
            Set Tenant ID and Project ID in the context bar above to search.
          </Banner>
        </div>
      )}

      <form
        className="global-search-page__form card"
        onSubmit={handleSubmit}
      >
        <label className="visually-hidden" htmlFor="global-search-page-input">
          Search query
        </label>
        <div className="global-search-page__input-row">
          <Icon.Search className="icon global-search-page__icon" />
          <input
            id="global-search-page-input"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="What would you like to know?"
            disabled={!ready || state.kind === "loading"}
            autoFocus
            autoComplete="off"
            data-testid="global-search-page-input"
          />
          <button
            type="submit"
            className="btn btn--primary"
            disabled={!ready || !query.trim() || state.kind === "loading"}
            data-testid="global-search-page-submit"
          >
            {state.kind === "loading" ? "Searching…" : "Search"}
          </button>
        </div>
      </form>

      {state.kind === "empty" && ready && (
        <div
          className="global-search-page__hint card"
          data-testid="global-search-page-empty"
        >
          <p>
            Enter a question above to query the active knowledge base.
            Sources and an optional retrieval-details drawer will appear
            here once a result is returned.
          </p>
        </div>
      )}

      {state.kind === "loading" && (
        <div
          className="global-search-page__loading card"
          data-testid="global-search-page-loading"
        >
          <Icon.Loader className="icon" />
          <span>Searching the knowledge base…</span>
        </div>
      )}

      {state.kind === "error" && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="err" title="Search failed">
            {state.message}
          </Banner>
        </div>
      )}

      {state.kind === "ready" && (
        <SearchResultView
          response={state.response}
          onOpenDocument={onOpenDocument}
          onOpenRun={onOpenRun}
        />
      )}
    </div>
  );
}
