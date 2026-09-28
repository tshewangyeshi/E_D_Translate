// Browser tests for the widget against real frameworks (S4.1, ER-16).
// Requirements: FR-210, FR-212, FR-214, FR-215, FR-510.
//
// jsdom proves the widget's logic. It cannot prove the thing most likely to go
// wrong in production: that React and Vue tolerate the widget having rewritten
// their text. Hydration compares the server's HTML against what the client
// renders, and a re-render diffs against nodes the widget has touched. Both
// complain in the console rather than throwing, so these tests treat any
// console error or hydration warning as a failure.

import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";

import { start } from "./server.mjs";

let base: string;
let close: () => void;

test.beforeAll(async () => {
  const { server, port } = await start();
  base = `http://127.0.0.1:${port}`;
  close = () => server.close();
});

test.afterAll(() => close?.());

/** Console output a framework uses to report a problem it did not throw for. */
function watchConsole(page: Page): { problems: string[] } {
  const problems: string[] = [];
  const suspicious = /hydrat|did not match|mismatch|Warning:|Failed to execute|removeChild|insertBefore/i;
  page.on("console", (message: ConsoleMessage) => {
    const text = message.text();
    if (message.type() === "error" || suspicious.test(text)) problems.push(`${message.type()}: ${text}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  return { problems };
}

const FIXTURES = ["react-csr", "react-ssr", "vue-csr", "vue-ssr"] as const;

for (const fixture of FIXTURES) {
  test.describe(fixture, () => {
    test(`fr210_translate_rerender_and_toggle_leave_${fixture.replace("-", "_")}_intact`, async ({
      page,
    }) => {
      const watch = watchConsole(page);
      await page.goto(`${base}/${fixture}`);

      // Wait for the framework to own the DOM before the widget touches it.
      await page.waitForFunction(() => typeof (window as never as { __rerender?: unknown }).__rerender === "function");
      const toggle = page.locator("[data-dz-control]");
      await expect(toggle).toBeVisible();
      expect(watch.problems, "problems before the widget did anything").toEqual([]);

      const intro = page.locator("#intro");
      await expect(intro).toContainText("Apply to renew your passport online.");

      // Translate.
      await toggle.click();
      await expect(intro).toContainText("DZ:Apply to renew your passport online.");
      expect(watch.problems, "problems after translating").toEqual([]);

      // FR-510: the Tier 1 line was refused and must still read English.
      await expect(page.locator("main p.legal")).toHaveText("Fees are non-refundable.");

      // A host re-render that does NOT touch translated text. Cheap, and it
      // catches a framework that crashes merely because the DOM changed.
      await page.evaluate(() => (window as never as { __rerender: () => void }).__rerender());
      await expect(page.locator("#counter")).toHaveText("Counter: 1");
      expect(watch.problems, "problems after a host re-render").toEqual([]);

      // The test that actually defends FR-210. The host now updates the very
      // text the widget rewrote. React and Vue write through the DOM node they
      // remember creating; if the widget replaced that node instead of setting
      // its value, the framework updates a node no longer in the document and
      // the change never reaches the reader -- silently, with no error anywhere.
      await page.evaluate(() =>
        (window as never as { __setIntro: (t: string) => void }).__setIntro("Host updated this"),
      );
      await expect(
        page.locator("#intro"),
        "the host update must reach the page after translation",
      ).toContainText("Host updated this");
      expect(watch.problems, "problems after the host rewrote translated text").toEqual([]);

      // Toggle back. The intro must keep the host's newer text rather than
      // being overwritten with our stale copy of the English (FR-212), while
      // every block the host did not touch returns to English.
      await toggle.click();
      await expect(
        page.locator("#intro"),
        "toggling back must not overwrite a host edit",
      ).toContainText("Host updated this");
      await expect(page.locator("main p").nth(1)).toHaveText("The renewal fee is Nu. 1,200.");
      expect(watch.problems, "problems after toggling back").toEqual([]);
    });

    test(`fr214_marks_machine_output_in_${fixture.replace("-", "_")}`, async ({ page }) => {
      await page.goto(`${base}/${fixture}`);
      await page.waitForFunction(() => typeof (window as never as { __rerender?: unknown }).__rerender === "function");
      await page.locator("[data-dz-control]").click();
      await expect(page.locator("#intro")).toHaveAttribute("lang", "dz-x-mtfrom-en");
      // The refused Tier 1 block carries no language claim at all.
      await expect(page.locator("main p.legal")).not.toHaveAttribute("lang", /dz/);
    });
  });
}

test("fr215_an_api_failure_leaves_the_page_in_english", async ({ page }) => {
  const watch = watchConsole(page);
  await page.route("**/v1/translate", (route) => route.abort("failed"));
  await page.goto(`${base}/react-ssr`);
  await page.waitForFunction(() => typeof (window as never as { __rerender?: unknown }).__rerender === "function");

  await page.locator("[data-dz-control]").click();
  await expect(page.locator("#intro")).toContainText("Apply to renew your passport online.");
  expect(watch.problems.filter((p) => !p.includes("Failed to load resource"))).toEqual([]);
});

test("fr216_no_control_is_offered_when_configuration_fails", async ({ page }) => {
  await page.route("**/v1/config**", (route) => route.fulfill({ status: 503, body: "" }));
  await page.goto(`${base}/react-ssr`);
  await page.waitForFunction(() => typeof (window as never as { __rerender?: unknown }).__rerender === "function");
  await expect(page.locator("[data-dz-control]")).toHaveCount(0);
  await expect(page.locator("#intro")).toContainText("Apply to renew your passport online.");
});
