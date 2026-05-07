/**
 * Results > Raw Artifacts tab.
 *
 * Paginated table of every artifact the run produced (regardless of
 * kind). Optional kind filter (drives the same `?kind=` query param
 * the backend uses). Click a row → opens `ArtifactDrawer` with an
 * inline preview / download.
 *
 * This is the "escape hatch" tab — when the user wants to look at
 * something that doesn't fit the Chunks / Assets / Graph specialised
 * views, this is where they go.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ReviewArtifactListQuery,
  ReviewArtifactPage,
  ReviewArtifactRecord,
} from "@/types/review";
import { ArtifactDrawer } from "./ArtifactDrawer";
import { formatBytes } from "./artifact-helpers";

interface RawArtifactsTabProps {
  runId: string;
}

const DEFAULT_PAGE_SIZE = 25;

export function RawArtifactsTab({ runId }: RawArtifactsTabProps) {
  const client = useClient();
  const [page, setPage] = useState(1);
  const [kindFilter, setKindFilter] = useState<string>("");

  const [pageData, setPageData] = useState<ReviewArtifactPage | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  const [openRecord, setOpenRecord] = useState<ReviewArtifactRecord | null>(null);

  // Reset page on filter change.
  useEffect(() => {
    setPage(1);
  }, [kindFilter]);

  const query: ReviewArtifactListQuery = useMemo(
    () => ({
      page,
      pageSize: DEFAULT_PAGE_SIZE,
      kind: kindFilter || undefined,
    }),
    [page, kindFilter],
  );

  useEffect(() => {
    let cancelled = false;
    setPageLoading(true);
    setPageError(null);
    void (async () => {
      try {
        const result = await client.listRunArtifacts(runId, query);
        if (!cancelled) setPageData(result);
      } catch (e) {
        if (!cancelled) {
          setPageError(e instanceof Error ? e.message : "Failed to load.");
        }
      } finally {
        if (!cancelled) setPageLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId, query]);

  // The kind filter dropdown derives its options from the items the
  // server returned for the current run. Using the artifact-counts
  // map from the summary would be more authoritative, but this keeps
  // the tab self-contained and the dropdown still reflects the
  // production reality on the first page load.
  //
  // Computed up here (above the error-state early return) because
  // hooks must run on every render in the same order — placing this
  // after the `pageError` short-circuit triggers a hooks-order
  // violation.
  const kindsSeen = useMemo(() => {
    const items = pageData?.items ?? [];
    const set = new Set<string>();
    for (const a of items) set.add(a.kind);
    return Array.from(set).sort();
  }, [pageData]);

  if (pageError) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load artifacts.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{pageError}</div>
      </div>
    );
  }

  const items = pageData?.items ?? [];
  const total = pageData?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / DEFAULT_PAGE_SIZE));

  return (
    <div className="results-raw">
      <div className="results-raw__toolbar">
        <label className="results-raw__filter">
          <span>Kind</span>
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
          >
            <option value="">(all kinds)</option>
            {kindsSeen.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <div className="results-raw__count">
          {pageLoading
            ? "Loading…"
            : `${total} artifact${total === 1 ? "" : "s"}`}
        </div>
      </div>

      {!pageLoading && items.length === 0 ? (
        <div className="results__empty results__empty--inline">
          No artifacts match the current filter.
        </div>
      ) : (
        <table className="results-raw__table">
          <thead>
            <tr>
              <th>Artifact</th>
              <th>Kind</th>
              <th>Size</th>
              <th>Created</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr
                key={a.artifactId}
                className={
                  openRecord?.artifactId === a.artifactId ? "is-active" : ""
                }
              >
                <td>
                  <span className="results-raw__id">{a.artifactId}</span>
                  <div className="results-raw__location">{a.location}</div>
                </td>
                <td>{a.kind}</td>
                <td>{formatBytes(a.byteSize)}</td>
                <td className="results-raw__date">
                  {a.createdAt
                    ? new Date(a.createdAt).toLocaleString()
                    : "—"}
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => setOpenRecord(a)}
                  >
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {totalPages > 1 ? (
        <div className="results-chunks__pager">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            disabled={page <= 1 || pageLoading}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ← Prev
          </button>
          <span className="results-chunks__pager-text">
            Page {page} of {totalPages}
          </span>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            disabled={page >= totalPages || pageLoading}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            Next →
          </button>
        </div>
      ) : null}

      <ArtifactDrawer
        runId={runId}
        record={openRecord}
        onClose={() => setOpenRecord(null)}
      />
    </div>
  );
}
