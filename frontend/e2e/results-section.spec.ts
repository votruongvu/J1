/**
 * Results section smoke — exercises the post-terminal review flow
 * end to end against the mock client.
 *
 *   1. Start the warnings scenario, confirm the plan, wait for terminal.
 *   2. Verify the Results section appears and the Overview tab shows
 *      the loaded summary (KPIs + step table + warnings list).
 *   3. Switch to Chunks → row click → drawer with Readable / Raw JSON
 *      toggle.
 *   4. Switch to Assets → at least one section renders with a card.
 *   5. Switch to Graph → entity + relation tables rendered (warnings
 *      scenario produces a populated graph in mock mode).
 *   6. Switch to Quality → confidence scorecard rendered.
 *   7. Switch to Raw artifacts → filter dropdown + at least one row.
 *   8. Verify the Graph tab shows the skipped state in the default
 *      scenario (where graph is policy-skipped).
 */

import { test, expect, type Page } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });
});

async function startScenarioAndCompleteRun(
  page: Page,
  scenario: "warnings" | "failure" | "review",
) {
  await page.goto("/");
  await page.getByRole("button", { name: /new ingestion run/i }).first().click();
  await page.locator("select").selectOption(scenario);
  await page
    .getByRole("button", { name: "Run demo document", exact: true })
    .click();
  const confirm = page.getByRole("button", { name: /confirm & run/i });
  await confirm.waitFor({ state: "visible", timeout: 15_000 });
  await confirm.click();
  // Wait for the run to reach a terminal state — `.stream-status--closed`
  // is the integration-level signal that the SSE timeline has wrapped.
  await expect(page.locator(".stream-status--closed")).toBeVisible({
    timeout: 60_000,
  });
}

test("Results > Overview shows KPIs + step table + warnings", async ({
  page,
}) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  await expect(results).toBeVisible();
  await expect(results.locator(".results-section__title")).toHaveText(
    "Results",
  );

  // Overview is the default tab. Wait for the KPI strip — this is the
  // signal that summary fetch completed.
  await expect(results.locator(".results-overview__kpis")).toBeVisible({
    timeout: 15_000,
  });

  // KPI strip carries the standard fields.
  const kpis = results.locator(".results-kpi");
  const kpiCount = await kpis.count();
  expect(kpiCount).toBeGreaterThanOrEqual(5);

  // Step table renders with the step rows from the mock summary.
  const stepRows = results.locator(".results-step-table tbody tr");
  await expect(stepRows.first()).toBeVisible();
  const rowCount = await stepRows.count();
  expect(rowCount).toBeGreaterThanOrEqual(2);

  // Warnings section appears (the warnings scenario surfaces
  // step.warning events, which the mock summary echoes).
  await expect(
    results.locator(".results-overview__warnings li").first(),
  ).toBeVisible();
});

test("Results > Chunks lists items + drawer toggles Readable / Raw JSON", async ({
  page,
}) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  await results
    .getByRole("tab", { name: "Chunks", exact: true })
    .click();

  // Chunks rows visible.
  const chunkRows = results.locator(".results-chunks__row");
  await expect(chunkRows.first()).toBeVisible({ timeout: 15_000 });
  expect(await chunkRows.count()).toBeGreaterThanOrEqual(3);

  // Click the first row → drawer opens.
  await chunkRows.first().getByRole("button").click();
  const drawer = page.locator(".drawer.is-open").first();
  await expect(drawer).toBeVisible();

  // Default is "Readable" — the body block is rendered.
  await expect(drawer.locator(".chunk-readable__body")).toBeVisible();

  // Switch to Raw JSON — the JsonView <pre.json> appears.
  await drawer.getByRole("button", { name: "Raw JSON" }).click();
  await expect(drawer.locator("pre.json")).toBeVisible();

  // Switch back to Readable — body block returns.
  await drawer.getByRole("button", { name: "Readable" }).click();
  await expect(drawer.locator(".chunk-readable__body")).toBeVisible();
});

test("Results > Assets renders at least one kind section + card", async ({
  page,
}) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  await results
    .getByRole("tab", { name: "Assets", exact: true })
    .click();

  const sections = results.locator(".results-assets__section");
  await expect(sections.first()).toBeVisible({ timeout: 15_000 });
  expect(await sections.count()).toBeGreaterThanOrEqual(1);

  const cards = results.locator(".results-asset-card");
  await expect(cards.first()).toBeVisible({ timeout: 15_000 });
});

test("Results > Graph renders entity + relation tables (populated scenario)", async ({
  page,
}) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  // Wait for the summary to populate availableViews so the Graph tab
  // becomes enabled (mock summary returns graph: { available: true }
  // only in the "warnings" scenario).
  await expect(results.locator(".results-overview__kpis")).toBeVisible({
    timeout: 15_000,
  });

  await results
    .getByRole("tab", { name: "Graph", exact: true })
    .click();

  // Two graph tables are visible — entities + relations.
  const tables = results.locator(".results-graph__table");
  await expect(tables.first()).toBeVisible({ timeout: 15_000 });
  expect(await tables.count()).toBe(2);

  // Search filter narrows the entity list.
  await results
    .locator(".results-graph__search")
    .fill("Alice");
  // "Alice" matches the PERSON:Alice entity AND the rel_001
  // relation that has Alice as its source. Both tables should
  // still show at least one row.
  await expect(
    results.locator(".results-graph__table").first().locator("tbody tr"),
  ).toHaveCount(1);
});

test("Results > Graph shows skipped reason in default scenario", async ({
  page,
}) => {
  // Default scenario (warnings is mocked-default for the smoke spec
  // above; here we explicitly use the `review` scenario which falls
  // through to the policy-skipped branch in the mock).
  // Actually the mock summary uses a different rule than getRunGraph:
  //   - summary.availableViews.graph.available is true only on warnings
  //   - getRunGraph returns populated only on warnings, unavailable
  //     ("policy") on default + review, "failure" string on failure
  // So review → graph tab is DISABLED in the run summary's tabs. We
  // can verify the disabled state instead.
  await startScenarioAndCompleteRun(page, "review");

  const results = page.locator(".results-section");
  await expect(results.locator(".results-overview__kpis")).toBeVisible({
    timeout: 15_000,
  });

  const graphTab = results.getByRole("tab", { name: "Graph", exact: true });
  await expect(graphTab).toHaveAttribute("aria-disabled", "true");
  // Tooltip carries the skipped reason — same copy the GraphTab body
  // would render for the unavailable state.
  await expect(graphTab).toHaveAttribute(
    "title",
    /policy/i,
  );
});

test("Results > Quality renders confidence scorecard", async ({ page }) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  await results
    .getByRole("tab", { name: "Quality", exact: true })
    .click();

  await expect(
    results.locator(".results-quality__scorecard"),
  ).toBeVisible({ timeout: 15_000 });
  // At least one confidence card with a percent value.
  await expect(
    results.locator(".results-conf-card__value").first(),
  ).toContainText("%");
});

test("Results > Raw Artifacts paginated table + filter", async ({ page }) => {
  await startScenarioAndCompleteRun(page, "warnings");

  const results = page.locator(".results-section");
  await results
    .getByRole("tab", { name: "Raw artifacts", exact: true })
    .click();

  const rows = results.locator(".results-raw__table tbody tr");
  await expect(rows.first()).toBeVisible({ timeout: 15_000 });
  expect(await rows.count()).toBeGreaterThanOrEqual(2);

  // Kind dropdown is populated with the kinds seen on the page.
  const select = results.locator(".results-raw__filter select");
  // Filter to a specific kind that the mock fixture surfaces. The
  // dropdown change triggers an async re-fetch, so wait for the
  // row count to reflect the filtered set (mock has exactly one
  // `enriched.tables` artifact).
  await select.selectOption("enriched.tables");
  await expect
    .poll(async () => await rows.count(), {
      timeout: 5_000,
      message: "kind filter never reduced the row count",
    })
    .toBe(1);
  // Confirm the surviving row's kind cell reads `enriched.tables`.
  await expect(
    results.locator(".results-raw__table tbody tr td:nth-child(2)"),
  ).toHaveText("enriched.tables");
});
