// Accessibility on the host pages (S4.4).
// Requirements: FR-210, NFR-300, NFR-401
//
// The widget adds a toggle, a notice and Dzongkha text to someone else's page.
// It must not make that page less accessible: axe-core, on each of the four
// fixture hosts, finds no violation with the widget that it did not find
// without it -- in English, and again in Dzongkha with the notice showing.
// Violations the host page has on its own are the host's, not counted here.

import AxeBuilder from "@axe-core/playwright";
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

/** Each violation as "rule @ element", so the same rule on a new element counts as new. */
async function violations(page: Page): Promise<Set<string>> {
  const results = await new AxeBuilder({ page }).analyze();
  const found = new Set<string>();
  for (const v of results.violations) {
    for (const node of v.nodes) found.add(`${v.id} @ ${node.target.join(" ")}`);
  }
  return found;
}

function added(withWidget: Set<string>, baseline: Set<string>): string[] {
  return [...withWidget].filter((v) => !baseline.has(v));
}

for (const fixture of ["react-csr", "react-ssr", "vue-csr", "vue-ssr"] as const) {
  test(`nfr401_the_widget_adds_no_accessibility_violation_${fixture.replace("-", "_")}`, async ({
    page,
  }) => {
    // Baseline: the same host, the widget never loaded.
    await page.route("**/widget/**", (route) => route.abort());
    await page.goto(`${base}/${fixture}`);
    await page.waitForFunction(() => typeof (window as never as { __rerender?: unknown }).__rerender === "function");
    const baseline = await violations(page);
    await page.unroute("**/widget/**");

    await page.goto(`${base}/${fixture}`);
    const toggle = page.locator("[data-dz-control]");
    await expect(toggle).toBeVisible();
    expect(added(await violations(page), baseline), "new violations in English").toEqual([]);

    await toggle.click();
    await expect(page.locator("#intro")).toContainText("DZ:");
    await expect(page.locator("[data-dz-notice]")).toBeVisible();
    expect(added(await violations(page), baseline), "new violations in Dzongkha").toEqual([]);
  });
}
