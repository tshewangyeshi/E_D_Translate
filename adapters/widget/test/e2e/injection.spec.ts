// Host-page safety: a hostile translation response must not execute (S4.4).
// Requirements: NFR-300, FR-210, FR-113.
//
// `docs/CLAUDE.md` names XSS through re-injected translations as one of three
// live threats. The threat model is specific and worth stating: the widget
// takes a response from the orchestrator, which took text from a model, and
// writes it into a government page. If any link in that chain is compromised
// -- a poisoned cache entry, a tampered response, a model coaxed into emitting
// markup -- the widget is the thing holding the knife.
//
// The defence is structural rather than filtering: translations are written
// with `nodeValue` and `setAttribute`, which cannot create an element or run a
// script whatever the string contains. These tests hold that structure to
// account by feeding it payloads that would execute if it ever changed to
// innerHTML. No sanitiser is involved, and none should be: sanitisers are
// bypassed, `nodeValue` is not parsed.

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

/** Payloads that run if the value is ever parsed as HTML instead of set as text. */
const PAYLOADS = [
  '<script>window.__pwned = 1;</script>',
  '<img src=x onerror="window.__pwned = 1">',
  '<svg/onload="window.__pwned = 1">',
  '"><script>window.__pwned = 1;</script>',
  "<iframe src=\"javascript:window.__pwned = 1\"></iframe>",
  '</p><script>window.__pwned = 1;</script><p>',
];

/** Answer every translate request with a hostile value. */
async function serveHostile(page: Page, payload: string): Promise<void> {
  await page.route("**/v1/translate", async (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      segments: { id: string; text: string }[];
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        model_version: "hostile",
        glossary_version: "hostile",
        segments: body.segments.map((s) => ({
          id: s.id,
          // Keep any inline markers the source had, or the widget refuses the
          // block for tag mismatch and the payload never gets near the DOM --
          // which would make this test pass for the wrong reason.
          text: (s.text.match(/⟦[^⟧]*⟧/g) ?? []).join("") + payload,
          status: "translated",
          origin: "mt",
        })),
      }),
    });
  });
}

for (const payload of PAYLOADS) {
  test(`nfr300_a_hostile_translation_is_not_executed: ${payload.slice(0, 28)}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await serveHostile(page, payload);
    await page.goto(`${base}/react-ssr`);
    await page.waitForFunction(
      () => typeof (window as never as { __rerender?: unknown }).__rerender === "function",
    );

    const scriptsBefore = await page.locator("script").count();
    await page.locator("[data-dz-control]").click();
    await page.waitForTimeout(500);

    // The payload never ran.
    expect(
      await page.evaluate(() => (window as never as { __pwned?: number }).__pwned),
      "the translation executed",
    ).toBeUndefined();

    // It created no elements: no new script tags, no injected image or frame.
    expect(await page.locator("script").count(), "a script element was created").toBe(
      scriptsBefore,
    );
    expect(await page.locator("#app img:not(#photo)").count()).toBe(0);
    expect(await page.locator("#app iframe, #app svg").count()).toBe(0);

    expect(errors, "the host page threw").toEqual([]);
  });
}

test("nfr300_the_payload_is_visible_as_literal_text", async ({ page }) => {
  // The other direction, and the one that proves the mechanism rather than
  // just the absence of harm: the characters are ON the page, as text. If the
  // widget were stripping or escaping them the payload could be absent for a
  // reason that would not hold for the next payload.
  await serveHostile(page, "<script>window.__pwned = 1;</script>");
  await page.goto(`${base}/react-ssr`);
  await page.waitForFunction(
    () => typeof (window as never as { __rerender?: unknown }).__rerender === "function",
  );
  await page.locator("[data-dz-control]").click();
  await page.waitForTimeout(500);

  await expect(page.locator("#app")).toContainText("<script>window.__pwned = 1;</script>");
  expect(await page.evaluate(() => (window as never as { __pwned?: number }).__pwned)).toBeUndefined();
});

test("fr113_a_hostile_attribute_translation_cannot_break_out", async ({ page }) => {
  // Attributes go through setAttribute, so a quote in the value is a quote in
  // the value. This would be an escape only if the widget ever built markup.
  await serveHostile(page, '" onerror="window.__pwned = 1" x="');
  await page.goto(`${base}/react-ssr`);
  await page.waitForFunction(
    () => typeof (window as never as { __rerender?: unknown }).__rerender === "function",
  );
  await page.locator("[data-dz-control]").click();
  await page.waitForTimeout(500);

  const photo = page.locator("#photo");
  await expect(photo).toHaveAttribute("alt", /onerror/); // stored verbatim
  expect(await photo.getAttribute("onerror"), "the payload became a real attribute").toBeNull();
  expect(await page.evaluate(() => (window as never as { __pwned?: number }).__pwned)).toBeUndefined();
});
