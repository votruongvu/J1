/**
 * NeedsAttentionPanel — concise list of operator-visible
 * warnings derived from the current document state.
 *
 * Renders nothing when the list is empty so the dashboard stays
 * clean on a healthy system. When there ARE items, each is
 * surfaced as a tinted line with the canonical kind/icon
 * mapping the rest of the app uses (warn → amber, err → red).
 *
 * No action buttons — operators navigate via Recent Runs / the
 * Documents page. Keeping this panel read-only stops the Home
 * page from accumulating one-off actions.
 */

import { Icon } from "@/components/icons";

import type { AttentionItem } from "./home-dashboard-helpers";


interface NeedsAttentionPanelProps {
  items: readonly AttentionItem[];
}


export function NeedsAttentionPanel({ items }: NeedsAttentionPanelProps) {
  if (items.length === 0) return null;
  return (
    <section
      className="card needs-attention-panel"
      data-testid="home-needs-attention"
    >
      <h3 className="card__title">Needs attention</h3>
      <ul className="needs-attention-panel__list">
        {items.map((item) => (
          <li
            key={item.id}
            className={`needs-attention-panel__item needs-attention-panel__item--${item.kind}`}
            data-testid={`needs-attention-${item.id}`}
          >
            {item.kind === "err" ? (
              <Icon.XCircle className="icon needs-attention-panel__icon" />
            ) : (
              <Icon.Alert className="icon needs-attention-panel__icon" />
            )}
            <span>{item.message}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
