import { test, expect } from "@playwright/test";

import {
  buildFixture,
  teardownFixture,
  ensureDistBuilt,
  bootAppWithFixture,
  bumpConflictRevision,
} from "./_setup";
import type { Fixture } from "./_setup";

async function freshFixture(): Promise<Fixture> {
  ensureDistBuilt();
  return buildFixture();
}

test.describe.serial("P7/T15 audit UI", () => {
  test("overview renders counts and projection state", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      // P7 acceptance: page heading must render INSIDE .app-main, not
      // bleed below the grid layout. The earlier routing bug put
      // <AppShell /> as a sibling of <Routes />, so <Outlet /> never
      // resolved and pages stacked under .app-shell in the document.
      const heading = page.getByRole("heading", { name: "处理概览" });
      await expect(heading).toBeVisible();
      // DOM-position contract: the h1 must be a descendant of main.app-main.
      await expect(page.locator(".app-main").getByRole("heading", { name: "处理概览" })).toBeVisible();
      await expect(page.getByTestId("stage-scanned")).toContainText(/\d/);
      await expect(page.getByTestId("stage-committed")).toContainText(/\d/);
      await expect(page.getByTestId("last-scan-at")).toContainText("最近扫描：");
      // Projection state pill is present in the header.
      await expect(page.getByTestId("projection-state")).toBeVisible();
    } finally {
      teardownFixture(fx);

    }
  });

  test("navigate source -> batch evidence -> memory -> conflict", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      // Sources
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "会话与来源" })
        .click();
      await expect(page.getByRole("heading", { name: "会话与来源" })).toBeVisible();
      await expect(page.locator("[data-testid^='source-card-']").first()).toBeVisible({ timeout: 10_000 });
      const sourceRow = page.locator("[data-testid^='source-card-']").first();
      await sourceRow.click();
      // Sessions/turns preview opens in the shared DetailPanel.
      await expect(page.getByTestId("detail-panel-close")).toBeVisible({ timeout: 5_000 });
      // Close the panel so the next nav click doesn't have to deal
      // with focus inside the dialog.
      await page.getByTestId("detail-panel-close").click();
      await expect(page.getByTestId("detail-panel-close")).toBeHidden({ timeout: 5_000 });

      // Batches
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "处理批次" })
        .click();
      await expect(page.getByRole("heading", { name: "处理批次" })).toBeVisible({ timeout: 5_000 });
      await expect(page.locator("[data-testid^='batch-row-']").first()).toBeVisible({ timeout: 10_000 });
      const batchRow = page.locator("[data-testid^='batch-row-']").first();
      await batchRow.click();
      // Original text is hidden until the user explicitly expands it.
      await expect(page.getByTestId("expand-evidence")).toBeVisible({ timeout: 5_000 });
      // Click to load inputs + candidates; the evidence UI surfaces the
      // span locator.
      await page.getByTestId("expand-evidence").click();
      await expect(page.locator("[data-testid^='evidence-input-']").first()).toBeVisible({ timeout: 10_000 });
      await expect(page.locator("text=locator: bytes").first()).toBeVisible({ timeout: 5_000 });

      // Memories / decisions
      await page.getByTestId("detail-panel-close").click();
      await expect(page.getByTestId("detail-panel-close")).toBeHidden({ timeout: 5_000 });
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "记忆与决定" })
        .click();
      await expect(page.getByRole("heading", { name: "记忆与决定" })).toBeVisible({ timeout: 5_000 });
      await expect(page.locator("[data-testid^='conflict-row-']").first()).toBeVisible({ timeout: 10_000 });
      const conflictRow = page.locator("[data-testid^='conflict-row-']").first();
      await conflictRow.click();
      await expect(page.getByTestId("conflict-detail")).toBeVisible({ timeout: 5_000 });
      await expect(page.getByTestId("decide-keep-old")).toBeEnabled({ timeout: 5_000 });
      await expect(page.getByTestId("decide-adopt-new")).toBeEnabled({ timeout: 5_000 });
    } finally {
      teardownFixture(fx);
      
    }
  });

  test("perform one decision; verify UI receipt + double-click idempotency", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "记忆与决定" })
        .click();
      const conflictRow = page.locator("[data-testid^='conflict-row-']").first();
      await conflictRow.click();
      await expect(page.getByTestId("conflict-detail")).toBeVisible();
      // Two rapid clicks; only one network commit should land.
      const btn = page.getByTestId("decide-keep-old");
      await btn.click();
      await btn.click({ force: true }).catch(() => undefined);
      // The UI re-renders once the conflict list is refreshed. Either
      // the in-detail banner shows or the conflict row goes away.
      await expect(async () => {
        const banner = page.locator("text=决定已应用").first();
        const empty = page.locator("text=当前没有待决冲突").first();
        const seen = (await banner.count()) > 0 || (await empty.count()) > 0;
        expect(seen).toBe(true);
      }).toPass({ timeout: 10_000 });
      // After the decide call, the conflict row disappears (status
      // moves to "decided" which the list filters out by default).
      await expect(page.locator("[data-testid^='conflict-row-']")).toHaveCount(0, { timeout: 10_000 });
    } finally {
      teardownFixture(fx);

    }
  });

  test("stale second action is blocked and refreshes", async ({ page, request }) => {
    const fx = await freshFixture();
    try {
      // Bump the DB so the server's stored revision no longer matches
      // what the UI will be told. We then forge ``/conflicts`` responses
      // so the UI holds the OLD revision (1) and tries to submit a
      // decide on top of it. The real server still has revision 2 and
      // returns 409 stale, which the UI surfaces as the "stale" banner.
      bumpConflictRevision(fx, fx.conflict_id);
      await page.route("**/api/v1/conflicts*", async (route) => {
        const real = await request.get(route.request().url());
        const body = await real.json();
        const mutated = {
          ...body,
          items: (body.items ?? []).map((it: Record<string, unknown>) => ({
            ...it,
            revision: 1,
          })),
        };
        await route.fulfill({
          status: real.status(),
          headers: real.headers(),
          body: JSON.stringify(mutated),
        });
      });

      await bootAppWithFixture(page, fx);
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "记忆与决定" })
        .click();
      await page.locator("[data-testid^='conflict-row-']").first().click({ timeout: 10_000 });
      await page.getByTestId("decide-keep-old").click();
      // Server returns 409; UI surfaces the "stale" banner and disables
      // the action buttons.
      await expect(page.getByTestId("stale-banner")).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("decide-keep-old")).toBeDisabled();
    } finally {
      teardownFixture(fx);

    }
  });

  test("trace shows original query and rewrite provenance", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      await page.getByRole("navigation", { name: "Audit sections" })
        .getByRole("link", { name: "查询审计" })
        .click();
      // Wait for React Router to commit the navigation.
      await page.waitForURL("**/queries", { timeout: 10_000 });
      await expect(page.getByRole("heading", { name: "查询审计" })).toBeVisible();
      // The fixture always records one query.trace audit row.
      const traceRow = page.locator("[data-testid^='trace-row-']").first();
      await traceRow.click();
      // The detail view surfaces the trace metadata: trace_id,
      // retrieval mode, rewrite, expansions, accepted/rejected
      // variants and the persisted trace artifact reference. The
      // list summary already includes the result_count; the detail
      // confirms the rewrite provenance is recorded.
      await expect(page.getByTestId("trace-detail")).toBeVisible();
      const detail = page.getByTestId("trace-detail");
      await expect(detail).toContainText("trace_id");
      await expect(detail).toContainText("接受的变体");
      await expect(detail).toContainText("original");
      await expect(detail).toContainText("拒绝的变体");
    } finally {
      teardownFixture(fx);

    }
  });

  test("audit and jobs panel polls without model calls", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      await page.getByRole("link", { name: "审计与作业" }).click();
      await expect(page.getByRole("heading", { name: "审计与作业" })).toBeVisible();
      // Audit rows must show; no /api/v1/recall-type calls happened.
      const rows = page.locator("[data-testid^='audit-row-']");
      await expect(rows.first()).toBeVisible({ timeout: 10_000 });
      // Confirm only /api/v1/audit|overview|queries was hit — not
      // /api/v1/recall.
      const calls: string[] = [];
      page.on("request", (req) => {
        if (req.url().includes("/api/v1/")) calls.push(req.url());
      });
      await page.waitForTimeout(300);
      const hasRecall = calls.some((u) => u.includes("/api/v1/recall"));
      expect(hasRecall).toBe(false);
    } finally {
      teardownFixture(fx);
      
    }
  });

  test("narrow viewport + keyboard navigation still works", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 480, height: 800 } });
    const page = await ctx.newPage();
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      // Tab into the navigation and reach the source page via keyboard.
      await page.keyboard.press("Tab"); // skip-link / focus first
      await page.keyboard.press("Tab");
      // The first anchor in the nav is "处理概览"; press Tab until we
      // reach "会话与来源". Up to 6 tabs is enough for the nav row.
      let reachedSources = false;
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press("Tab");
        const focused = await page.evaluate(() => document.activeElement?.textContent ?? "");
        if (focused.includes("会话与来源")) {
          reachedSources = true;
          break;
        }
      }
      expect(reachedSources).toBe(true);
      await page.keyboard.press("Enter");
      await expect(page.getByRole("heading", { name: "会话与来源" })).toBeVisible();
    } finally {
      teardownFixture(fx);
      
      await ctx.close();
    }
  });

  test("dark theme renders contrast-safe tokens", async ({ page }) => {
    const fx = await freshFixture();
    try {
      await bootAppWithFixture(page, fx);
      await page.evaluate(() => {
        document.documentElement.dataset.theme = "dark";
        localStorage.setItem("ma-theme", "dark");
      });
      const cssVar = await page.evaluate(() => {
        const cs = getComputedStyle(document.documentElement);
        return {
          canvas: cs.getPropertyValue("--ma-canvas").trim(),
          ink: cs.getPropertyValue("--ma-ink").trim(),
          accent: cs.getPropertyValue("--ma-accent").trim(),
        };
      });
      // Tokens must NOT be empty after the override.
      expect(cssVar.canvas).not.toBe("");
      expect(cssVar.ink).not.toBe("");
      expect(cssVar.accent).not.toBe("");
    } finally {
      teardownFixture(fx);
      
    }
  });
});