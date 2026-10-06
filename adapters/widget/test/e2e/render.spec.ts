// Dzongkha rendering conformance (S6.1, S6.2, S6.3).
// Requirements: FR-340, FR-341, FR-160
//
// Runs in Chromium (project "fixtures"), Firefox and WebKit ("render-*").
// The conformance page (conformance.html) installs the shipped typography and
// font exactly as main.ts does, on a host that -- like the G2C portal -- is
// itself marked Dzongkha and pins a font with no Tibetan in it.

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

async function conformance(page: Page): Promise<void> {
  await page.goto(`${base}/conformance`);
  await page.waitForFunction(() => (window as never as { __ready?: boolean }).__ready === true);
  // Fail loudly, by name, if the font is missing: a layout assertion failing
  // later for that reason would send someone looking in the wrong place.
  const loaded = await page.evaluate(async () => {
    try {
      const faces = await document.fonts.load('16px "dzweb Dzongkha"', "ཀ");
      return faces.length > 0 && document.fonts.check('16px "dzweb Dzongkha"', "ཀ");
    } catch {
      return false; // a 404 rejects rather than resolving empty
    }
  });
  if (!loaded) throw new Error("S6.3: the Dzongkha font (dist/fonts/dzweb-dzongkha.woff2) did not load");
}

function px(value: string): number {
  return Number.parseFloat(value);
}

test("fr340_the_self_hosted_font_renders_written_dzongkha", async ({ page }) => {
  await conformance(page);
  const family = await page.locator("#stacks").evaluate((e) => getComputedStyle(e).fontFamily);
  expect(family.replace(/"/g, "").startsWith("dzweb Dzongkha")).toBe(true); // WebKit drops the quotes
  // A pinned host font on a child does not take the Tibetan back (the portal's case).
  await page.locator("#stacks").evaluate((e) => {
    const span = document.createElement("span");
    span.style.fontFamily = '"Times New Roman"';
    span.id = "pinned";
    span.textContent = "ཀ་";
    e.appendChild(span);
  });
  expect(await page.locator("#pinned").evaluate((e) => getComputedStyle(e).fontFamily)).toContain("dzweb Dzongkha");
});

test("fr340_a_host_page_marked_dzongkha_is_not_restyled", async ({ page }) => {
  await conformance(page);
  const host = await page.locator("#host").evaluate((e) => {
    const s = getComputedStyle(e);
    return { family: s.fontFamily, size: s.fontSize, lineHeight: s.lineHeight };
  });
  expect(host.family).toContain("Times New Roman");
  expect(host.size).toBe("16px");
  expect(px(host.lineHeight)).toBeCloseTo(24, 0);
});

test("fr341_type_scale_from_the_original_size_without_compounding", async ({ page }) => {
  await conformance(page);
  const size = (id: string) => page.locator(id).evaluate((e) => getComputedStyle(e).fontSize);
  expect(px(await size("#stacks"))).toBeCloseTo(16 * 1.3, 1);
  expect(px(await size("#heading"))).toBeCloseTo(24 * 1.3, 1); // from its own size, not the body's
  expect(px(await size("#outer"))).toBeCloseTo(16 * 1.3, 1);
  expect(px(await size("#inner"))).toBeCloseTo(16 * 1.3, 1); // not 1.3 x 1.3
  const lineHeight = await page.locator("#stacks").evaluate((e) => getComputedStyle(e).lineHeight);
  expect(px(lineHeight)).toBeCloseTo(16 * 1.3 * 2, 0);
});

test("s61_english_inside_dzongkha_keeps_english_type", async ({ page }) => {
  await conformance(page);
  const s = await page.locator("#english").evaluate((e) => {
    const style = getComputedStyle(e);
    return { family: style.fontFamily, size: style.fontSize, lineHeight: style.lineHeight };
  });
  expect(s.family).toContain("Times New Roman"); // the host's own font, not the Dzongkha one
  expect(px(s.size)).toBeCloseTo(16, 1); // its own size, not 1.3x
  expect(s.lineHeight).toBe("normal");
});

test("fr341_a_host_can_set_the_type_scale", async ({ page }) => {
  await conformance(page);
  await page.addStyleTag({ content: ":root { --dzweb-dz-scale: 1; --dzweb-dz-line-height: 1.8; }" });
  const s = await page.locator("#stacks").evaluate((e) => {
    const style = getComputedStyle(e);
    return { fontSize: style.fontSize, lineHeight: style.lineHeight };
  });
  expect(px(s.fontSize)).toBeCloseTo(16, 1);
  expect(px(s.lineHeight)).toBeCloseTo(16 * 1.8, 0);
});

test("fr341_stacked_syllables_render_unclipped", async ({ page }) => {
  await conformance(page);
  for (const id of ["#stacks", "#heading", "#label", "#th", "#button", "#mixed"]) {
    const fits = await page.locator(id).evaluate((e) => {
      const s = getComputedStyle(e);
      const canvas = document.createElement("canvas").getContext("2d")!;
      canvas.font = `${s.fontSize} "dzweb Dzongkha"`;
      const ink = canvas.measureText(e.textContent ?? "");
      const inkHeight = ink.actualBoundingBoxAscent + ink.actualBoundingBoxDescent;
      const lineHeight = Number.parseFloat(s.lineHeight);
      // The tallest stack's ink fits in the line box, and nothing is cut off.
      return {
        inkHeight,
        lineHeight,
        clipped: e.scrollHeight > e.clientHeight + 1 && s.overflow !== "visible",
      };
    });
    expect(fits.inkHeight, `${id}: ink ${fits.inkHeight}px in a ${fits.lineHeight}px line`).toBeLessThanOrEqual(
      fits.lineHeight,
    );
    expect(fits.clipped, `${id} is clipped`).toBe(false);
  }
});

test("fr160_long_dzongkha_wraps_inside_its_container", async ({ page }) => {
  await conformance(page);
  const box = await page.locator("#narrow").evaluate((e) => ({
    scroll: e.scrollWidth,
    client: e.clientWidth,
    lines: Math.round(e.getBoundingClientRect().height / Number.parseFloat(getComputedStyle(e).lineHeight)),
  }));
  expect(box.scroll).toBeLessThanOrEqual(box.client);
  expect(box.lines).toBeGreaterThan(3); // it wrapped, rather than being cut off
});

test("fr340_the_widget_installs_typography_and_sizes_what_it_writes", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "the framework fixtures run in Chromium");
  await page.goto(`${base}/react-csr`);
  await page.waitForFunction(() => typeof (window as never as { __route?: unknown }).__route === "function");
  const intro = page.locator("#intro");
  const before = await intro.evaluate((e) => getComputedStyle(e).fontSize);
  await page.locator("[data-dz-control]").click();
  await expect(intro).toContainText("DZ:");
  expect(await page.locator("style[data-dz-style]").count()).toBe(1);
  expect(px(await intro.evaluate((e) => getComputedStyle(e).fontSize))).toBeCloseTo(px(before) * 1.3, 1);

  await page.locator("[data-dz-control]").click(); // back to English
  await expect(intro).not.toContainText("DZ:");
  expect(await intro.evaluate((e) => getComputedStyle(e).fontSize)).toBe(before);
  expect(await intro.evaluate((e) => e.hasAttribute("data-dz-written") || e.hasAttribute("style"))).toBe(false);
});
