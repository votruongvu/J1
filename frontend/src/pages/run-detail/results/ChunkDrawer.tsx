/**
 * Chunk detail drawer.
 *
 * Two view modes selected via top-level toggle:
 *   - "Readable" — full body in a pre-wrap block, plus lineage panel.
 *   - "Raw JSON" — entire detail DTO via `JsonView`.
 *
 * Lineage panel is always visible above the body (or below the JSON
 * — whichever the active view places more naturally) so reviewers
 * can see source artifact / document / stage at a glance.
 */

import { useState } from "react";
import { Icon } from "@/components/icons";
import { JsonView } from "@/components/JsonView";
import type { ReviewChunkDetail } from "@/types/review";

interface ChunkDrawerProps {
  open: boolean;
  chunkId: string | null;
  detail: ReviewChunkDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}

type ViewMode = "readable" | "raw";

export function ChunkDrawer({
  open,
  chunkId,
  detail,
  loading,
  error,
  onClose,
}: ChunkDrawerProps) {
  const [view, setView] = useState<ViewMode>("readable");

  return (
    <div
      className={`drawer ${open ? "is-open" : ""}`}
      role="dialog"
      aria-hidden={!open}
      aria-label={chunkId ? `Chunk ${chunkId}` : "Chunk detail"}
    >
      <div className="drawer__head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Icon.Code className="icon" />
          <strong>{chunkId ?? "Chunk"}</strong>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onClose}
          aria-label="Close drawer"
        >
          <Icon.X className="icon-sm" />
        </button>
      </div>

      <div className="drawer__tabs">
        <button
          type="button"
          className={`drawer__tab ${view === "readable" ? "is-active" : ""}`}
          onClick={() => setView("readable")}
        >
          Readable
        </button>
        <button
          type="button"
          className={`drawer__tab ${view === "raw" ? "is-active" : ""}`}
          onClick={() => setView("raw")}
        >
          Raw JSON
        </button>
      </div>

      <div className="drawer__body">
        {error ? (
          <div className="results__empty" role="alert">
            <strong>Couldn&apos;t load chunk.</strong>
            <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
          </div>
        ) : loading || !detail ? (
          <div className="results__empty" aria-busy="true">
            Loading chunk…
          </div>
        ) : view === "readable" ? (
          <ReadableView detail={detail} />
        ) : (
          <JsonView value={detail} />
        )}
      </div>
    </div>
  );
}

function ReadableView({ detail }: { detail: ReviewChunkDetail }) {
  return (
    <div className="chunk-readable">
      <Lineage detail={detail} />
      {detail.title ? (
        <h3 className="chunk-readable__title">{detail.title}</h3>
      ) : null}
      <pre className="chunk-readable__body">{detail.body}</pre>
      {detail.linkedAssets.length > 0 ? (
        <div className="chunk-readable__assets">
          <div className="chunk-readable__assets-title">Linked assets</div>
          <ul>
            {detail.linkedAssets.map((a) => (
              <li key={a.artifactId}>
                <code>{a.artifactId}</code>
                {a.kind ? (
                  <span className="results-chunks__meta">{a.kind}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Lineage({ detail }: { detail: ReviewChunkDetail }) {
  const docIds = (detail.lineage["documentIds"] as unknown) ?? null;
  const sourceArtifact =
    (detail.lineage["sourceArtifactId"] as string | undefined) ??
    detail.sourceArtifactId ??
    null;
  const stage = (detail.lineage["stage"] as string | undefined) ?? null;
  const pageRange = pageRangeLabel(detail.pageStart, detail.pageEnd);

  return (
    <dl className="chunk-readable__lineage">
      {pageRange ? (
        <>
          <dt>Pages</dt>
          <dd>{pageRange}</dd>
        </>
      ) : null}
      {detail.section ? (
        <>
          <dt>Section</dt>
          <dd>{detail.section}</dd>
        </>
      ) : null}
      {detail.tokenCount != null ? (
        <>
          <dt>Tokens</dt>
          <dd>{detail.tokenCount}</dd>
        </>
      ) : null}
      {detail.confidence != null ? (
        <>
          <dt>Confidence</dt>
          <dd>{(detail.confidence * 100).toFixed(0)}%</dd>
        </>
      ) : null}
      {Array.isArray(docIds) && docIds.length > 0 ? (
        <>
          <dt>Documents</dt>
          <dd>{(docIds as unknown[]).map(String).join(", ")}</dd>
        </>
      ) : null}
      {sourceArtifact ? (
        <>
          <dt>Source artifact</dt>
          <dd>
            <code>{sourceArtifact}</code>
          </dd>
        </>
      ) : null}
      {stage ? (
        <>
          <dt>Stage</dt>
          <dd>{stage}</dd>
        </>
      ) : null}
    </dl>
  );
}

function pageRangeLabel(
  start: number | null | undefined,
  end: number | null | undefined,
): string | null {
  if (start == null && end == null) return null;
  if (start != null && end != null && start !== end) return `${start}–${end}`;
  return String(start ?? end);
}
