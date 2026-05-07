/**
 * Results > Assets tab.
 *
 * Shows the run's enrichment outputs that benefit from inline
 * preview: tables, visuals (images), formulas. Each card carries
 * a small thumbnail / structured preview + a download button.
 *
 * Per-kind partitioning: the tab issues three parallel
 * `listRunArtifacts` calls (one per asset kind). pageSize=100
 * is generous enough to surface every asset for any practical
 * run; if a future deployment produces more, swap in pagination
 * per section.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ReviewArtifactRecord,
} from "@/types/review";
import { ArtifactPreview } from "./ArtifactPreview";
import { formatBytes } from "./artifact-helpers";

interface AssetsTabProps {
  runId: string;
}

const ASSET_KINDS: Array<{ kind: string; label: string }> = [
  { kind: "enriched.tables", label: "Tables" },
  { kind: "enriched.visuals", label: "Visuals" },
  { kind: "enriched.formulas", label: "Formulas" },
];

export function AssetsTab({ runId }: AssetsTabProps) {
  const client = useClient();
  const [bucketsByKind, setBucketsByKind] = useState<
    Record<string, ReviewArtifactRecord[]>
  >({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const results = await Promise.all(
          ASSET_KINDS.map((k) =>
            client
              .listRunArtifacts(runId, { kind: k.kind, pageSize: 100 })
              .then((p) => [k.kind, p.items] as const),
          ),
        );
        if (cancelled) return;
        const buckets: Record<string, ReviewArtifactRecord[]> = {};
        for (const [kind, items] of results) buckets[kind] = items;
        setBucketsByKind(buckets);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load assets.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  const totalCount = useMemo(
    () =>
      Object.values(bucketsByKind).reduce(
        (sum, arr) => sum + arr.length,
        0,
      ),
    [bucketsByKind],
  );

  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load assets.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading assets…
      </div>
    );
  }
  if (totalCount === 0) {
    return (
      <div className="results__empty">
        No assets were produced for this run.
      </div>
    );
  }

  return (
    <div className="results-assets">
      {ASSET_KINDS.map(({ kind, label }) => {
        const items = bucketsByKind[kind] ?? [];
        if (items.length === 0) return null;
        return (
          <section key={kind} className="results-assets__section">
            <h3 className="results-assets__section-title">
              {label}{" "}
              <span className="results-assets__section-count">
                ({items.length})
              </span>
            </h3>
            <div className="results-assets__grid">
              {items.map((a) => (
                <AssetCard key={a.artifactId} runId={runId} record={a} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function AssetCard({
  runId,
  record,
}: {
  runId: string;
  record: ReviewArtifactRecord;
}) {
  const caption =
    (record.metadata["caption"] as string | undefined) ??
    (record.metadata["filename"] as string | undefined) ??
    null;
  return (
    <article className="results-asset-card">
      <div className="results-asset-card__preview">
        <ArtifactPreview runId={runId} record={record} compact />
      </div>
      <div className="results-asset-card__meta">
        <div className="results-asset-card__id">{record.artifactId}</div>
        {caption ? (
          <div className="results-asset-card__caption">{caption}</div>
        ) : null}
        <div className="results-asset-card__byline">
          {record.kind} · {formatBytes(record.byteSize)}
        </div>
      </div>
    </article>
  );
}
