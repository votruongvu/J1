/**
 * Results > Content Inventory tab.
 *
 * Renders the parsed-content manifest from
 * `GET /ingestion-runs/{id}/parsed-content`. Available as soon as
 * the compile activity emits the manifest artifact, even while
 * downstream stages (enrich / graph / index) are still running.
 *
 * Three states:
 *   * `status="completed"` → counts + items table.
 *   * `status="empty"` → "Parser ran but found nothing extractable."
 *   * `status="unavailable"` → backend's `unavailableReason` copy.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  ContentInventory,
  ContentInventoryItem,
} from "@/types/review";

interface ContentInventoryTabProps {
  runId: string;
}

type TypeFilter =
  | "all"
  | "text"
  | "table"
  | "image"
  | "formula"
  | "heading"
  | "other";

export function ContentInventoryTab({ runId }: ContentInventoryTabProps) {
  const client = useClient();
  const [inventory, setInventory] = useState<ContentInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await client.getRunContentInventory(runId);
        if (!cancelled) setInventory(result);
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof Error ? e.message : "Failed to load content inventory.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  const filteredItems = useMemo(() => {
    if (!inventory) return [];
    const needle = search.trim().toLowerCase();
    return inventory.items.filter((item) => {
      if (typeFilter !== "all" && item.type !== typeFilter) return false;
      if (!needle) return true;
      return (
        item.itemId.toLowerCase().includes(needle) ||
        (item.preview ?? "").toLowerCase().includes(needle) ||
        (item.location ?? "").toLowerCase().includes(needle)
      );
    });
  }, [inventory, typeFilter, search]);

  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load content inventory.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading || !inventory) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading content inventory…
      </div>
    );
  }

  if (inventory.status === "unavailable") {
    return (
      <div className="results__empty">
        <strong>Content inventory not available</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 6 }}>
          {inventory.unavailableReason ??
            "No parsed-content manifest is available for this run."}
        </div>
      </div>
    );
  }

  const { summary, source } = inventory;
  const isEmpty = inventory.status === "empty";

  return (
    <div className="results-content-inventory">
      {/* Source / parser metadata strip */}
      <section className="results-content-inventory__source">
        <SourceField label="Document">
          {inventory.documentName ?? inventory.documentId ?? "—"}
        </SourceField>
        <SourceField label="Parser">
          {source.parser ?? "—"}
          {source.parserVersion ? (
            <span style={{ color: "var(--text-muted)", marginLeft: 4 }}>
              v{source.parserVersion}
            </span>
          ) : null}
        </SourceField>
        <SourceField label="Parse method">
          {source.parseMethod ?? "—"}
        </SourceField>
        {source.profile ? (
          <SourceField label="Profile">{source.profile}</SourceField>
        ) : null}
      </section>

      {/* Summary scorecards */}
      <section className="results-content-inventory__summary">
        <SummaryCard label="Pages" value={summary.pageCount} />
        <SummaryCard label="Text blocks" value={summary.textBlockCount} />
        <SummaryCard label="Tables" value={summary.tableCount} />
        <SummaryCard label="Images" value={summary.imageCount} />
        <SummaryCard label="Formulas" value={summary.formulaCount} />
        {summary.headingCount != null ? (
          <SummaryCard label="Headings" value={summary.headingCount} />
        ) : null}
        <SummaryCard
          label="Total items"
          value={summary.totalItems}
          tone="accent"
        />
      </section>

      {isEmpty ? (
        <div className="results__empty">
          Parser completed but did not surface a structured content
          inventory for this run.
        </div>
      ) : null}

      {/* Items table — only when we have something to show */}
      {inventory.items.length > 0 ? (
        <section className="results-content-inventory__items">
          <div className="results-graph__toolbar">
            <input
              type="search"
              placeholder="Search id / preview / location…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="results-graph__search"
              aria-label="Filter content items"
            />
            <label className="results-graph__type-filter">
              <span>Type</span>
              <select
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value as TypeFilter)}
              >
                <option value="all">(all types)</option>
                <option value="text">text</option>
                <option value="table">table</option>
                <option value="image">image</option>
                <option value="formula">formula</option>
                <option value="heading">heading</option>
                <option value="other">other</option>
              </select>
            </label>
            <div className="results-graph__counts">
              <span>
                {filteredItems.length} / {inventory.items.length} items
              </span>
            </div>
          </div>
          {filteredItems.length === 0 ? (
            <div className="results__empty results__empty--inline">
              No items match the current filter.
            </div>
          ) : (
            <table className="results-graph__table" role="table">
              <thead>
                <tr>
                  <th>Id</th>
                  <th>Type</th>
                  <th>Page</th>
                  <th>Preview</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((item) => (
                  <ItemRow key={item.itemId} item={item} />
                ))}
              </tbody>
            </table>
          )}
        </section>
      ) : null}
    </div>
  );
}

function SourceField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="results-content-inventory__source-field">
      <span className="results-content-inventory__source-label">{label}</span>
      <span className="results-content-inventory__source-value">{children}</span>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | null | undefined;
  tone?: "accent";
}) {
  return (
    <div
      className={`results-conf-card${tone === "accent" ? " results-conf-card--good" : ""}`}
    >
      <div className="results-conf-card__label">{label}</div>
      <div className="results-conf-card__value">{value ?? "—"}</div>
    </div>
  );
}

function ItemRow({ item }: { item: ContentInventoryItem }) {
  return (
    <tr>
      <td>
        <code className="results-graph__id">{item.itemId}</code>
      </td>
      <td>
        <span className="results-tag results-tag--info">{item.type}</span>
      </td>
      <td>{item.page != null ? item.page : "—"}</td>
      <td className="results-graph__desc">
        {item.preview ? (
          <span title={item.preview}>{truncatePreview(item.preview, 120)}</span>
        ) : (
          <span className="results-graph__placeholder">—</span>
        )}
      </td>
      <td>
        {item.skipped ? (
          <span
            className="results-tag results-tag--warning"
            title={item.skipReason ?? undefined}
          >
            skipped
          </span>
        ) : item.passedToEnrichment ? (
          <span className="results-tag results-tag--info">enriched</span>
        ) : (
          <span className="results-graph__placeholder">—</span>
        )}
      </td>
    </tr>
  );
}

function truncatePreview(preview: string, maxLen: number): string {
  if (preview.length <= maxLen) return preview;
  return preview.slice(0, maxLen).trimEnd() + "…";
}
