/**
 * Top-of-app primary navigation.
 *
 * Three sections, in the order the spec calls for:
 *
 *   Home          — the dashboard landing page
 *   Search        — the dedicated Global Search page
 *   Documents     — the document list
 *
 * Renders BELOW the ContextBar so the tenant/project surface
 * stays prominent on every page. Owns no state — the parent
 * (App.tsx) passes the current section identifier + a single
 * navigation callback so this component stays trivially
 * testable.
 */

import { Icon } from "./icons";


export type MainNavSection = "home" | "search" | "documents";


interface MainNavProps {
  active: MainNavSection;
  onNavigate: (section: MainNavSection) => void;
}


interface NavItem {
  id: MainNavSection;
  label: string;
  icon: keyof typeof Icon;
  testid: string;
}


// Stable order — operators learn to find sections by position.
// Pinning the order in a constant means a future refactor that
// reorders them is intentional.
const _NAV_ITEMS: readonly NavItem[] = [
  { id: "home", label: "Home", icon: "Home", testid: "main-nav-home" },
  { id: "search", label: "Search", icon: "Search", testid: "main-nav-search" },
  { id: "documents", label: "Documents", icon: "File", testid: "main-nav-documents" },
];


export function MainNav({ active, onNavigate }: MainNavProps) {
  return (
    <nav className="main-nav" aria-label="Primary" data-testid="main-nav">
      <ul className="main-nav__list">
        {_NAV_ITEMS.map((item) => {
          const IconEl = Icon[item.icon];
          const isActive = item.id === active;
          return (
            <li key={item.id} className="main-nav__item">
              <button
                type="button"
                className={
                  "main-nav__link"
                  + (isActive ? " main-nav__link--active" : "")
                }
                onClick={() => onNavigate(item.id)}
                aria-current={isActive ? "page" : undefined}
                data-testid={item.testid}
              >
                <IconEl className="icon-sm" />
                <span>{item.label}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
