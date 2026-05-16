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
    // Compile-Graph framing: the tab represents the graph/index
    // RAGAnything/LightRAG produced during compile. "Unavailable"
    // is rephrased so operators don't read it as a skipped
    // downstream stage — under the new architecture there's no
    // separate Build-Knowledge-Graph step, so the right framing
    // is "no compile graph artifact was found", not "graph step
    // was skipped".
    return (
      <div className="results__empty" data-testid="results-graph-empty">
        <strong>No compile graph artifact for this run</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 6 }}>
          {snapshot.unavailable.reason}
        </div>
        <div
          style={{
            color: "var(--text-muted)",
            marginTop: 6,
            fontSize: 12,
          }}
        >
          The compile graph is produced by RAGAnything/LightRAG
          during compile. If it&apos;s missing on a successful run,
          inspect the raw Artifacts tab for the run&apos;s
          <code style={{ margin: "0 4px" }}>graph_json</code>
          record.
        </div>
      </div>
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
