/**
 * Execution Console smoke — exercises the user-visible flow end to
 * end against the mock client. The mock client emits a scripted
 * event timeline (compile / enrich / graph / index) that closes
 * cleanly on `run.completed` (with the default "warnings" scenario,
 * `run.completed` is the terminal). The test asserts:
 *
 *   1. The user can set Tenant + Project in the context bar.
 *   2. Clicking "New ingestion run" → "Run demo document" creates a
 *      run and navigates to the detail page.
 *   3. The run header shows the document name + status badge.
 *   4. The timeline appends events live (without a manual refresh).
 *   5. The stream pill flips from "Live" to "Stream closed" once
 *      the terminal event arrives.
 *
 * If any of those break, the integration is broken — independent of
 * what unit tests say about individual modules.
 */

import { test, expect } from "@playwright/test";

const MOCK_TENANT = "demo-tenant";
const MOCK_PROJECT = "demo-project";

test.beforeEach(async ({ page }) => {
  // Force every spec to start with a clean localStorage so a stale
  // `j1.mode=live` from a prior run can't bleed in.
  await page.addInitScript(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });
});

test("upload → run detail → live timeline → terminal close (mock)", async ({
  page,
}) => {
  await page.goto("/");

  // The default state ships with Tenant/Project pre-populated to
  // the mock constants, but verify defensively — a regression that
  // empties them would show as a missing-context banner. The
  // ContextBar inputs are placeholder-tagged (`tenant-id` /
  // `project-id`) rather than label-associated, so we locate by
  // placeholder.
  await expect(page.getByPlaceholder("tenant-id")).toHaveValue(MOCK_TENANT);
  await expect(page.getByPlaceholder("project-id")).toHaveValue(MOCK_PROJECT);

  // Mock mode pill is visible (we never went near "Live API"). The
  // toggle button's aria-label is "Toggle data source" so the
  // accessible-name lookup doesn't see "Mock mode" — match the
  // visible text instead.
  await expect(page.getByText("Mock mode", { exact: true })).toBeVisible();

  // Navigate to the upload page.
  await page.getByRole("button", { name: /new ingestion run/i }).first().click();
  await expect(
    page.getByRole("heading", { name: /new ingestion run/i }),
  ).toBeVisible();

  // Trigger the demo upload. `exact: true` disambiguates from the
  // dropzone div which also has role="button" and contains the
  // string "Run demo document" inside its accessible name.
  await page
    .getByRole("button", { name: "Run demo document", exact: true })
    .click();

  // Run-detail header is up: document name + a status badge.
  const runHeader = page.locator(".run-hero");
  await expect(runHeader).toBeVisible();

  // The mock script halts on a `gate: "confirm"` entry until the
  // user confirms. Wait for the confirm button to render, then
  // click it — without this the timeline parks at PLAN_READY and
  // never reaches a terminal event.
  const confirm = page.getByRole("button", {
    name: /confirm & run/i,
  });
  await confirm.waitFor({ state: "visible", timeout: 15_000 });
  await confirm.click();

  // The mock client emits its scripted timeline asynchronously over
  // ~20 s. Wait for the timeline to populate at least a handful of
  // events — this is the realtime guarantee.
  const timelineItems = page.locator(".timeline .tl-item");
  await expect
    .poll(async () => await timelineItems.count(), {
      timeout: 15_000,
      message: "timeline never populated past the initial events",
    })
    .toBeGreaterThanOrEqual(5);

  // Stream pill says "Live" while events are flowing.
  await expect(page.locator(".stream-status--live")).toBeVisible();

  // Wait for the terminal event. The default mock scenario is
  // "warnings" → run completes with warnings.
  await expect(page.locator(".stream-status--closed")).toBeVisible({
    timeout: 60_000,
  });

  // Header status badge reflects a terminal state. The mock
  // scenario "warnings" maps the lowercase wire status
  // succeeded_with_warnings → COMPLETED_WITH_WARNINGS in the FE
  // translator; the badge label is "Completed · warnings".
  await expect(
    page.locator(".run-hero .badge").filter({
      hasText: /completed/i,
    }),
  ).toBeVisible();

  // The plan card has rendered the execution plan with at least
  // one stage group. (The mock plan has all four.)
  await expect(page.locator(".stage-group").first()).toBeVisible();
});

async function startScenario(
  page: import("@playwright/test").Page,
  scenario: "warnings" | "failure" | "review",
) {
  await page.goto("/");
  await page.getByRole("button", { name: /new ingestion run/i }).first().click();
  await page.locator("select").selectOption(scenario);
  await page
    .getByRole("button", { name: "Run demo document", exact: true })
    .click();
  // Confirm the plan to unblock the gated timeline.
  const confirm = page.getByRole("button", { name: /confirm & run/i });
  await confirm.waitFor({ state: "visible", timeout: 15_000 });
  await confirm.click();
}

test("run detail surfaces failure scenario panel", async ({ page }) => {
  await startScenario(page, "failure");

  // The failure scenario emits `run.failed`; the primary status
  // panel should render the failed variant and the stream closes.
  await expect(page.locator(".psp--failed")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".stream-status--closed")).toBeVisible();
});

test("run detail surfaces human-review scenario", async ({ page }) => {
  await startScenario(page, "review");

  // The review scenario emits `human_review.required`; FE flips
  // status to `AWAITING_HUMAN_REVIEW` and PrimaryStatusPanel renders
  // the review variant. The mock script continues past the review
  // event and the stream closes when the script ends.
  await expect(page.locator(".psp--review")).toBeVisible({ timeout: 60_000 });
});
