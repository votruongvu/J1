/**
 * Results > Chunks tab.
 *
 * Paginated chunk list with an optional confidence floor filter.
 * Each row shows page range, section, token count, confidence, and
 * a short preview. Click a row to open `ChunkDrawer` for the full
 * body + lineage in either Readable or Raw JSON view.
 *
 * Owns its own data state — list cached per (page, pageSize, filter)
 * tuple so flipping filters doesn't bust the cache for previously
 * loaded combinations. Detail data is fetched on demand by the
 * drawer.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ReviewChunkDetail,
  ReviewChunkListQuery,
  ReviewChunkPage,
  ReviewChunkPreview,
} from "@/types/review";
import { ChunkDrawer } from "./ChunkDrawer";

interface ChunksTabProps {
  runId: string;
}

const DEFAULT_PAGE_SIZE = 25;

export function ChunksTab({ runId }: ChunksTabProps) {
  const client = useClient();
  const [page, setPage] = useState(1);
  const [pageSize] = useState(DEFAULT_PAGE_SIZE);
  // Confidence floor in percent (UI-friendly), translated to a 0..1
  // float for the API. `null` means "no filter" — shows every chunk
  // including ones without a confidence score.
  const [minConfidencePct, setMinConfidencePct] = useState<number | null>(null);

  const [pageData, setPageData] = useState<ReviewChunkPage | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  const [openChunkId, setOpenChunkId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewChunkDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const query: ReviewChunkListQuery = useMemo(
    () => ({
      page,
      pageSize,
      minConfidence:
        minConfidencePct != null ? minConfidencePct / 100 : undefined,
    }),
    [page, pageSize, minConfidencePct],
  );

  // Reset page to 1 when the filter changes — otherwise a stale page
  // index can land on an empty slice.
  useEffect(() => {
    setPage(1);
  }, [minConfidencePct]);

  useEffect(() => {
    let cancelled = false;
    setPageLoading(true);
    setPageError(null);
    void (async () => {
      try {
        const result = await client.listRunChunks(runId, query);
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

  const openChunk = (chunkId: string) => {
    setOpenChunkId(chunkId);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    void (async () => {
      try {
        const d = await client.getRunChunk(runId, chunkId);
        setDetail(d);
      } catch (e) {
        setDetailError(e instanceof Error ? e.message : "Failed to load.");
      } finally {
        setDetailLoading(false);
      }
    })();
  };

  const closeDrawer = () => {
    setOpenChunkId(null);
    setDetail(null);
    setDetailError(null);
  };

  if (pageError) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load chunks.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{pageError}</div>
      </div>
    );
  }

  const items = pageData?.items ?? [];
  const total = pageData?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="results-chunks">
      <div className="results-chunks__toolbar">
        <label className="results-chunks__filter">
          <span>Min. confidence</span>
          <select
            value={minConfidencePct == null ? "" : String(minConfidencePct)}
            onChange={(e) =>
              setMinConfidencePct(e.target.value === "" ? null : Number(e.target.value))
            }
          >
            <option value="">(no filter)</option>
            <option value="50">≥ 50%</option>
            <option value="60">≥ 60%</option>
            <option value="70">≥ 70%</option>
            <option value="80">≥ 80%</option>
            <option value="90">≥ 90%</option>
          </select>
        </label>
        <div className="results-chunks__count">
          {pageLoading ? "Loading…" : `${total} chunk${total === 1 ? "" : "s"}`}
        </div>
      </div>

      {!pageLoading && items.length === 0 ? (
        <div className="results__empty results__empty--inline">
          No chunks match the current filter.
        </div>
      ) : (
        <ul className="results-chunks__list">
          {items.map((c) => (
            <ChunkRow
              key={c.chunkId}
              chunk={c}
              active={c.chunkId === openChunkId}
              onSelect={() => openChunk(c.chunkId)}
            />
          ))}
        </ul>
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

      <ChunkDrawer
        open={openChunkId != null}
        chunkId={openChunkId}
        detail={detail}
        loading={detailLoading}
        error={detailError}
        onClose={closeDrawer}
      />
    </div>
  );
}

interface ChunkRowProps {
  chunk: ReviewChunkPreview;
  active: boolean;
  onSelect: () => void;
}

function ChunkRow({ chunk, active, onSelect }: ChunkRowProps) {
  const pageBadge = pageRangeLabel(chunk.pageStart, chunk.pageEnd);
  const conf = chunk.confidence != null
    ? `${(chunk.confidence * 100).toFixed(0)}%`
    : null;
  return (
    <li className={`results-chunks__row ${active ? "is-active" : ""}`}>
      <button
        type="button"
        className="results-chunks__row-button"
        onClick={onSelect}
        aria-pressed={active}
      >
        <div className="results-chunks__row-head">
          <span className="results-chunks__chunk-id">{chunk.chunkId}</span>
          {pageBadge ? (
            <span className="results-chunks__meta">{pageBadge}</span>
          ) : null}
          {chunk.section ? (
            <span className="results-chunks__meta">{chunk.section}</span>
          ) : null}
          {chunk.tokenCount != null ? (
            <span className="results-chunks__meta">
              {chunk.tokenCount} tok
            </span>
          ) : null}
          {conf ? (
            <span
              className={`results-tag results-tag--${
                (chunk.confidence ?? 0) < 0.6
                  ? "warning"
                  : (chunk.confidence ?? 0) < 0.8
                    ? "info"
                    : "info"
              }`}
            >
              {conf}
            </span>
          ) : null}
          {chunk.linkedAssets.length > 0 ? (
            <span className="results-chunks__meta">
              {chunk.linkedAssets.length} linked
            </span>
          ) : null}
        </div>
        <div className="results-chunks__row-preview">{chunk.preview}</div>
      </button>
    </li>
  );
}

function pageRangeLabel(
  start: number | null | undefined,
  end: number | null | undefined,
): string | null {
  if (start == null && end == null) return null;
  if (start != null && end != null && start !== end) return `pp. ${start}–${end}`;
  return `p. ${start ?? end}`;
}
