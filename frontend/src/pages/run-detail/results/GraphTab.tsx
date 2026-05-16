/**
 * Results > Graph tab.
 *
 * Renders the neutral graph snapshot returned by
 * `GET /ingestion-runs/{id}/graph`:
 *
 * - When `unavailable.reason` is set, show the empty state with
 * the reason (single source of truth: matches
 * `availableViews.graph.reason` in the run summary).
 * - When entities/relations were truncated, show a banner above
 * the tables with the cap that was applied.
 * - Always show two tables (entities + relations) with a shared
 * search box and an entity-type facet. Graph visualisation is
 * out of scope for v1 — tables are sufficient for review work
 * and don't pull in a viz dependency.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ReviewGraphEntity,
  ReviewGraphRelation,
  ReviewGraphSnapshot,
} from "@/types/review";

interface GraphTabProps {
  runId: string;
}

export function GraphTab({ runId }: GraphTabProps) {
  const client = useClient();
  const [snapshot, setSnapshot] = useState<ReviewGraphSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await client.getRunGraph(runId);
        if (!cancelled) setSnapshot(result);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load graph.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  // Derived: types seen in the entity list, used to populate the
  // type-facet dropdown. Hoisted above any conditional return so
  // hooks order stays stable.
  const entityTypes = useMemo(() => {
    const set = new Set<string>();
    for (const e of snapshot?.entities ?? []) {
      if (e.type) set.add(e.type);
    }
    return Array.from(set).sort();
  }, [snapshot]);

  const filteredEntities = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (snapshot?.entities ?? []).filter((e) => {
      if (typeFilter && e.type !== typeFilter) return false;
      if (!needle) return true;
      return (
        e.id.toLowerCase().includes(needle) ||
        e.label.toLowerCase().includes(needle) ||
        (e.description ?? "").toLowerCase().includes(needle) ||
        (e.type ?? "").toLowerCase().includes(needle)
      );
    });
  }, [snapshot, search, typeFilter]);

  const filteredRelations = useMemo(() => {
    const needle = search.trim().toLowerCase();
    // The type-facet filter doesn't apply to relations directly —
    // instead, when a type is selected we keep relations whose
    // EITHER endpoint matches an entity of that type.
    const typeAllowedEntityIds = typeFilter
      ? new Set(
          (snapshot?.entities ?? [])
            .filter((e) => e.type === typeFilter)
            .map((e) => e.id),
        )
      : null;
    return (snapshot?.relations ?? []).filter((r) => {
      if (
        typeAllowedEntityIds &&
        !typeAllowedEntityIds.has(r.sourceEntityId) &&
        !typeAllowedEntityIds.has(r.targetEntityId)
      ) {
        return false;
      }
      if (!needle) return true;
      return (
        r.id.toLowerCase().includes(needle) ||
        r.sourceEntityId.toLowerCase().includes(needle) ||
        r.targetEntityId.toLowerCase().includes(needle) ||
        (r.label ?? "").toLowerCase().includes(needle) ||
        (r.description ?? "").toLowerCase().includes(needle)
      );
    });
  }, [snapshot, search, typeFilter]);

  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load graph.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading || !snapshot) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading graph…
      </div>
    );
  }
  if (snapshot.unavailable) {
    // Compile-Graph fallback view. The strict graph-snapshot view
    // requires a registered `graph_json` artifact, which the
    // simplified compile flow does not produce — LightRAG writes
    // its graph data into the per-snapshot workdir instead. We
    // surface a lightweight summary from `compile_result_summary`
    // so the tab is informative rather than a dead end.
    return (
      <CompileGraphSummaryFallback
        runId={runId}
        reason={snapshot.unavailable.reason}
      />
    );
  }

  const truncatedEither =
    snapshot.truncated.entities || snapshot.truncated.relations;

  return (
    <div className="results-graph">
      {truncatedEither ? <TruncationBanner snapshot={snapshot} /> : null}

      <div className="results-graph__toolbar">
        <input
          type="search"
          placeholder="Search id / label / description…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="results-graph__search"
          aria-label="Filter entities and relations"
        />
        {entityTypes.length > 0 ? (
          <label className="results-graph__type-filter">
            <span>Type</span>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value="">(all types)</option>
              {entityTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="results-graph__counts">
          <span>
            {filteredEntities.length} / {snapshot.stats.entityCount} entities
          </span>
          <span>
            {filteredRelations.length} / {snapshot.stats.relationCount} relations
          </span>
        </div>
      </div>

      <section className="results-graph__section">
        <h3 className="results-graph__section-title">Entities</h3>
        {filteredEntities.length === 0 ? (
          <div className="results__empty results__empty--inline">
            No entities match the current filter.
          </div>
        ) : (
          <table className="results-graph__table" role="table">
            <thead>
              <tr>
                <th>Id</th>
                <th>Label</th>
                <th>Type</th>
                <th>Description</th>
                <th>Source chunks</th>
              </tr>
            </thead>
            <tbody>
              {filteredEntities.map((e) => (
                <EntityRow key={e.id} entity={e} />
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="results-graph__section">
        <h3 className="results-graph__section-title">Relations</h3>
        {filteredRelations.length === 0 ? (
          <div className="results__empty results__empty--inline">
            No relations match the current filter.
          </div>
        ) : (
          <table className="results-graph__table" role="table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Target</th>
                <th>Label</th>
                <th>Weight</th>
                <th>Description</th>
                <th>Source chunks</th>
              </tr>
            </thead>
            <tbody>
              {filteredRelations.map((r) => (
                <RelationRow key={r.id} relation={r} />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function TruncationBanner({ snapshot }: { snapshot: ReviewGraphSnapshot }) {
  const bits: string[] = [];
  if (snapshot.truncated.entities) {
    bits.push(
      `entities (${snapshot.stats.entityCount} total, capped at ${snapshot.truncated.limits.maxNodes})`,
    );
  }
  if (snapshot.truncated.relations) {
    bits.push(
      `relations (${snapshot.stats.relationCount} total, capped at ${snapshot.truncated.limits.maxEdges})`,
    );
  }
  return (
    <div className="results-graph__banner" role="status">
      <strong>Graph too large to render fully.</strong> Showing a partial
      view: {bits.join(" and ")}. Tables only — for the complete graph,
      download the underlying artifacts from the Raw Artifacts tab.
    </div>
  );
}

function EntityRow({ entity }: { entity: ReviewGraphEntity }) {
  return (
    <tr>
      <td>
        <code className="results-graph__id">{entity.id}</code>
      </td>
      <td>{entity.label}</td>
      <td>
        {entity.type ? (
          <span className="results-tag results-tag--info">{entity.type}</span>
        ) : (
          <span className="results-graph__placeholder">—</span>
        )}
      </td>
      <td className="results-graph__desc">{entity.description ?? "—"}</td>
      <td className="results-graph__chunk-list">
        {sourceChunkLabel(entity.sourceChunkIds)}
      </td>
    </tr>
  );
}

function RelationRow({ relation }: { relation: ReviewGraphRelation }) {
  return (
    <tr>
      <td>
        <code className="results-graph__id">{relation.sourceEntityId}</code>
      </td>
      <td>
        <code className="results-graph__id">{relation.targetEntityId}</code>
      </td>
      <td>
        {relation.label ? (
          <span className="results-tag results-tag--info">{relation.label}</span>
        ) : (
          <span className="results-graph__placeholder">—</span>
        )}
      </td>
      <td>{relation.weight != null ? relation.weight.toFixed(2) : "—"}</td>
      <td className="results-graph__desc">{relation.description ?? "—"}</td>
      <td className="results-graph__chunk-list">
        {sourceChunkLabel(relation.sourceChunkIds)}
      </td>
    </tr>
  );
}

function sourceChunkLabel(ids: string[]): string {
  if (ids.length === 0) return "—";
  if (ids.length <= 3) return ids.join(", ");
  return `${ids.slice(0, 3).join(", ")} +${ids.length - 3}`;
}


// ---- Compile Graph Summary fallback -----------------------------


/**
 * Operator-readable explanation for the typical case where compile
 * produced a graph inside LightRAG's workspace but didn't register
 * a `graph_json` artifact in J1's artifact registry. The fallback
 * reads `compile_result_summary` (which is always written when
 * compile succeeds) and projects whatever graph-relevant fields it
 * has — compile engine, chunks count, graph artifact refs (when
 * the vendor surfaced them), detected content types — so the tab
 * is informative even without a registered graph snapshot.
 *
 * Tests drive this component through the exported helper
 * `projectCompileGraphSummary` to keep the data-shape contract
 * stable across compile_result_summary schema additions.
 */
interface CompileGraphSummary {
  compileEngine: string;
  engineVersion: string | null;
  status: string | null;
  chunksCount: number | null;
  pageCount: number | null;
  detectedContentTypes: string[];
  graphArtifactRefs: string[];
  indexArtifactRefs: string[];
}


export function projectCompileGraphSummary(
  payload: unknown,
): CompileGraphSummary {
  const obj = (payload && typeof payload === "object")
    ? payload as Record<string, unknown>
    : {};
  const strOrNull = (v: unknown): string | null => {
    if (typeof v !== "string") return null;
    const s = v.trim();
    return s ? s : null;
  };
  const numOrNull = (v: unknown): number | null => {
    if (typeof v !== "number" || Number.isNaN(v)) return null;
    return v;
  };
  const arrOfStr = (v: unknown): string[] => {
    if (!Array.isArray(v)) return [];
    return v.filter((x): x is string => typeof x === "string");
  };
  return {
    compileEngine: strOrNull(obj.compile_engine) ?? "raganything",
    engineVersion: strOrNull(obj.engine_version),
    status: strOrNull(obj.status),
    chunksCount: numOrNull(obj.chunks_count),
    pageCount: numOrNull(obj.page_count),
    detectedContentTypes: arrOfStr(obj.detected_content_types),
    graphArtifactRefs: arrOfStr(obj.graph_artifact_refs),
    indexArtifactRefs: arrOfStr(obj.index_artifact_refs),
  };
}


interface CompileGraphSummaryFallbackProps {
  runId: string;
  reason: string;
}


function CompileGraphSummaryFallback({
  runId, reason,
}: CompileGraphSummaryFallbackProps) {
  const client = useClient();
  const [summary, setSummary] = useState<CompileGraphSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        // `compile_result_summary` is written by the
        // ProcessingService whenever compile succeeds. The list
        // call is cheap (one JSONL page) and the content fetch is
        // bytes-only — no LLM, no orchestration.
        const page = await client.listRunArtifacts(runId, {
          kind: "compile_result_summary",
        });
        const artifact = page.items[0];
        if (!artifact) {
          if (!cancelled) {
            setSummary(null);
            setLoading(false);
          }
          return;
        }
        const content = await client.getRunArtifactContent(
          runId, artifact.artifactId,
        );
        const text = await content.blob.text();
        const parsed = JSON.parse(text) as unknown;
        if (!cancelled) {
          setSummary(projectCompileGraphSummary(parsed));
        }
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof Error ? e.message : "Failed to load.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [client, runId]);

  return (
    <div
      className="results__empty results-graph__summary-fallback"
      data-testid="results-graph-summary-fallback"
    >
      <strong>Compile Graph Summary</strong>
      <p style={{ color: "var(--text-muted)", marginTop: 6 }}>
        The base graph/index is produced by RAGAnything/LightRAG
        during compile and lives inside the run&apos;s LightRAG
        workspace. No <code>graph_json</code> artifact is
        registered in J1&apos;s artifact registry for this run.
      </p>

      {loading && (
        <p
          className="muted"
          style={{ marginTop: 12 }}
          aria-busy="true"
        >
          Loading compile summary…
        </p>
      )}

      {error && (
        <p style={{ color: "var(--error-fg)", marginTop: 12 }}>
          Couldn&apos;t load compile summary: {error}
        </p>
      )}

      {summary && (
        <dl
          className="kv results-graph__summary-kv"
          data-testid="results-graph-summary-kv"
          style={{ marginTop: 12 }}
        >
          <dt>Compile engine</dt>
          <dd>
            <code>{summary.compileEngine}</code>
            {summary.engineVersion && (
              <span className="muted"> v{summary.engineVersion}</span>
            )}
          </dd>
          <dt>Status</dt>
          <dd>{summary.status ?? "—"}</dd>
          <dt>Chunks compiled</dt>
          <dd>
            {summary.chunksCount == null
              ? "—"
              : summary.chunksCount.toLocaleString()}
          </dd>
          <dt>Pages</dt>
          <dd>
            {summary.pageCount == null
              ? "—"
              : summary.pageCount.toLocaleString()}
          </dd>
          <dt>Detected content types</dt>
          <dd>
            {summary.detectedContentTypes.length === 0
              ? "—"
              : summary.detectedContentTypes.join(", ")}
          </dd>
          <dt>Graph artifact refs</dt>
          <dd>
            {summary.graphArtifactRefs.length === 0
              ? (
                <span className="muted">
                  No graph artifacts surfaced by the vendor for
                  this run.
                </span>
              )
              : (
                <ul className="results-graph__ref-list">
                  {summary.graphArtifactRefs.map((ref) => (
                    <li key={ref}>
                      <code>{ref}</code>
                    </li>
                  ))}
                </ul>
              )}
          </dd>
          <dt>Index artifact refs</dt>
          <dd>
            {summary.indexArtifactRefs.length === 0
              ? <span className="muted">—</span>
              : (
                <ul className="results-graph__ref-list">
                  {summary.indexArtifactRefs.map((ref) => (
                    <li key={ref}>
                      <code>{ref}</code>
                    </li>
                  ))}
                </ul>
              )}
          </dd>
        </dl>
      )}

      <p
        className="muted"
        style={{ marginTop: 16, fontSize: 12 }}
        data-testid="results-graph-summary-reason"
      >
        Graph snapshot reason: {reason}
      </p>
    </div>
  );
}
