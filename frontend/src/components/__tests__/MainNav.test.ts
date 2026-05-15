/**
 * Static-markup tests for the primary navigation.
 *
 * Pin the order + the active-state class so a refactor that
 * reorders sections or drops the highlight is a deliberate
 * change, not an accident.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { MainNav } from "../MainNav";


describe("MainNav", () => {
  it("renders the three sections in declared order", () => {
    const html = renderToStaticMarkup(
      createElement(MainNav, { active: "home", onNavigate: () => {} }),
    );
    const homeIdx = html.indexOf("main-nav-home");
    const searchIdx = html.indexOf("main-nav-search");
    const docsIdx = html.indexOf("main-nav-documents");
    expect(homeIdx).toBeGreaterThan(-1);
    expect(searchIdx).toBeGreaterThan(-1);
    expect(docsIdx).toBeGreaterThan(-1);
    // Order: home → search → documents (matches the spec).
    expect(homeIdx).toBeLessThan(searchIdx);
    expect(searchIdx).toBeLessThan(docsIdx);
  });

  it("marks the active section with the active class + aria-current", () => {
    const html = renderToStaticMarkup(
      createElement(MainNav, { active: "search", onNavigate: () => {} }),
    );
    const searchLink = html.match(
      /<button[^>]*main-nav-search[^>]*>[\s\S]*?<\/button>/,
    )?.[0];
    expect(searchLink).toBeDefined();
    expect(searchLink).toContain("main-nav__link--active");
    expect(searchLink).toContain('aria-current="page"');
    // Other sections must NOT carry the active marker.
    const homeLink = html.match(
      /<button[^>]*main-nav-home[^>]*>[\s\S]*?<\/button>/,
    )?.[0];
    expect(homeLink).not.toContain("main-nav__link--active");
    expect(homeLink).not.toContain('aria-current="page"');
  });

  it("labels every section with business-friendly copy", () => {
    const html = renderToStaticMarkup(
      createElement(MainNav, { active: "home", onNavigate: () => {} }),
    );
    expect(html).toContain(">Home<");
    expect(html).toContain(">Search<");
    expect(html).toContain(">Documents<");
  });
});
