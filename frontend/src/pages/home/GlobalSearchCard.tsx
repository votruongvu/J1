/**
 * Top-of-Home card: a prominent search input that hands off to
 * the Global Search page on submit. The card itself does NOT
 * execute the query — keeping search execution in one place
 * (the Global Search page) means the Home dashboard stays a
 * thin overview surface and we don't duplicate loading / error
 * state machines.
 *
 * Helper text intentionally business-friendly. "Active indexed
 * knowledge" matches the project_active scope the Global Search
 * page sends, so the operator's mental model lines up with the
 * backend behaviour.
 */

import { useState } from "react";
import type { FormEvent } from "react";

import { Icon } from "@/components/icons";


interface GlobalSearchCardProps {
  /** Owner (HomeDashboard) navigates to the Global Search page.
   * Passing the query through here lets the search page render
   * it pre-filled in the input + execute on mount. */
  onSubmit: (query: string) => void;
  /** True iff the dashboard knows search will yield nothing
   * (no indexed documents). Disables the submit button and
   * shows a short hint instead of an empty result. */
  disabled?: boolean;
  /** Optional copy override for the disabled hint — the
   * NeedsAttention panel already names the root cause, so the
   * card stays short. */
  disabledHint?: string;
}


export function GlobalSearchCard({
  onSubmit,
  disabled = false,
  disabledHint,
}: GlobalSearchCardProps) {
  const [query, setQuery] = useState("");
  const trimmed = query.trim();
  const canSubmit = !disabled && trimmed.length > 0;

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit(trimmed);
  };

  return (
    <section
      className="card global-search-card"
      data-testid="home-global-search-card"
    >
      <header className="global-search-card__header">
        <h2>Search the knowledge base</h2>
        <p className="global-search-card__hint">
          Ask across all active indexed knowledge.
        </p>
      </header>
      <form
        className="global-search-card__form"
        onSubmit={handleSubmit}
      >
        <label className="visually-hidden" htmlFor="global-search-input">
          Search query
        </label>
        <div className="global-search-card__input-row">
          <Icon.Search className="global-search-card__icon" />
          <input
            id="global-search-input"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="What would you like to know?"
            disabled={disabled}
            data-testid="home-global-search-input"
            autoComplete="off"
          />
          <button
            type="submit"
            className="btn btn--primary"
            disabled={!canSubmit}
            data-testid="home-global-search-submit"
          >
            Search
          </button>
        </div>
      </form>
      {disabled && (
        <p
          className="global-search-card__disabled-hint"
          data-testid="home-global-search-disabled-hint"
        >
          {disabledHint
            ?? "Search will become available once at least one document is indexed."}
        </p>
      )}
    </section>
  );
}
