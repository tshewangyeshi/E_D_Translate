// Toggle and persistence in a real browser (S4.3).
// Requirements: FR-212, FR-213 · [ER-19]
//
// jsdom proves the bookkeeping. Three things only a browser shows: the choice
// carried from one page of the site to the next, a host framework changing a
// fee while Dzongkha is on, and whether a long SPA session leaves detached
// DOM text behind once the garbage collector has run.

import { expect, test, type Page } from "@playwright/test";

import { start } from "./server.mjs";

let base: string;
let close: () => void;

test.beforeAll(async () => {
  const { server, port } = await start();
  base = `http://127.0.0.1:${port}`;
  close = () => server.close();
});

test.afterAll(() => close?.());

async function ready(page: Page): Promise<void> {
  await page.waitForFunction(() => typeof (window as never as { __route?: unknown }).__route === "function");
  await expect(page.locator("[data-dz-control]")).toBeVisible();
}

test("fr213_the_choice_carries_across_pages_of_the_same_site", async ({ page }) => {
  await page.goto(`${base}/react-csr`);
  await ready(page);
  await page.locator("[data-dz-control]").click();
  await expect(page.locator("#intro")).toContainText("DZ:");

  // Another page, same origin: Dzongkha without a click.
  await page.goto(`${base}/vue-csr`);
  await ready(page);
  await expect(page.locator("#intro")).toContainText("DZ:");

  // And back to English, which also carries.
  await page.locator("[data-dz-control]").click();
  await expect(page.locator("#intro")).not.toContainText("DZ:");
  await page.goto(`${base}/react-csr`);
  await ready(page);
  await page.waitForTimeout(1500); // long enough for an unwanted auto-translate
  await expect(page.locator("#intro")).not.toContainText("DZ:");
});

for (const fixture of ["react-csr", "vue-csr"] as const) {
  test(`fr212_regression_a_fee_changed_while_dzongkha_is_on_${fixture.replace("-", "_")}`, async ({ page }) => {
    await page.goto(`${base}/${fixture}`);
    await ready(page);
    const set = (text: string) =>
      page.evaluate((t) => (window as never as { __setIntro: (x: string) => void }).__setIntro(t), text);
    await set("The renewal fee is Nu. 500, payable at");
    await page.locator("[data-dz-control]").click();
    await expect(page.locator("#intro")).toContainText("DZ:The renewal fee is Nu. 500");

    await set("The renewal fee is Nu. 600, payable at"); // the host updates the fee
    await expect(page.locator("#intro")).toContainText("Nu. 600");
    await page.locator("[data-dz-control]").click(); // back to English

    await expect(page.locator("#intro")).toContainText("The renewal fee is Nu. 600, payable at");
    await expect(page.locator("#intro")).not.toContainText("Nu. 500");
    await expect(page.locator("#intro")).not.toContainText("DZ:");
  });

  test(`er19_fifty_route_changes_leave_no_detached_text_${fixture.replace("-", "_")}`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto(`${base}/${fixture}`);
    await ready(page);
    await page.locator("[data-dz-control]").click();
    await expect(page.locator("#intro")).toContainText("DZ:");

    const cdp = await page.context().newCDPSession(page);
    await cdp.send("HeapProfiler.enable");

    /** Text nodes still in memory after a full GC that are no longer in the document. */
    async function detachedText(): Promise<number> {
      await cdp.send("HeapProfiler.collectGarbage");
      const proto = await cdp.send("Runtime.evaluate", { expression: "Text.prototype" });
      const found = await cdp.send("Runtime.queryObjects", { prototypeObjectId: proto.result.objectId! });
      const counted = await cdp.send("Runtime.callFunctionOn", {
        objectId: found.objects.objectId!,
        functionDeclaration: "function () { return this.filter((t) => !t.isConnected).length; }",
        returnByValue: true,
      });
      await cdp.send("Runtime.releaseObject", { objectId: found.objects.objectId! });
      return counted.result.value as number;
    }

    async function routes(from: number, to: number): Promise<void> {
      for (let n = from; n < to; n += 1) {
        await page.evaluate((r) => (window as never as { __route: (x: number) => void }).__route(r), n);
        await expect(page.locator("p.added").first()).toContainText(`DZ:Route ${n}, block 0.`);
      }
    }

    await routes(0, 10);
    const after10 = await detachedText();
    await routes(10, 50);
    const after50 = await detachedText();

    // Each route drops 20 translated blocks. A widget that kept them would leave
    // 800 more detached Text nodes after 40 more routes; none may accumulate.
    // Checked 2026-10-05: a build holding blocks strongly left 980 after 50
    // routes and failed here; this widget leaves 0. The fixtures key added
    // blocks by content, so a route change really replaces elements.
    expect(after50 - after10, `detached Text: ${after10} after 10 routes, ${after50} after 50`).toBeLessThan(20);
  });
}
