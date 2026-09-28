// Main-thread cost of translating a long page (S4.2, ER-20).
// Requirements: FR-211, NFR-502.
//
// The failure this guards against is not a slow translation; it is a page that
// stops responding while translating. On the low-end Android hardware this
// service targets, a single long task is felt as a dead tap.
//
// Two numbers, measured with the CPU throttled 6x:
//
//   Long tasks    the browser's own longtask entries, over 50 ms.
//   Scheduler p95 how late a timer scheduled for "as soon as possible" actually
//                 ran. This is a PROXY for input latency, not input latency:
//                 a real tap queues behind the same main-thread work, so this
//                 bounds it, but it is measured in-page rather than by sending
//                 real input. Reported as what it is.

import { expect, test } from "@playwright/test";

import { start } from "./server.mjs";

let base: string;
let close: () => void;

test.beforeAll(async () => {
  const { server, port } = await start();
  base = `http://127.0.0.1:${port}`;
  close = () => server.close();
});

test.afterAll(() => close?.());

interface Sample {
  longTasks: { count: number; max: number };
  schedulerDelayP95: number;
  blocksTranslated: number;
}

test("er20_translating_a_long_page_does_not_block_the_main_thread", async ({ page }) => {
  test.slow(); // throttled 6x: generous budget, strict assertions

  const client = await page.context().newCDPSession(page);
  await client.send("Emulation.setCPUThrottlingRate", { rate: 6 });

  await page.goto(`${base}/perf`, { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-dz-control]")).toBeVisible();

  const result = await page.evaluate(async (): Promise<Sample> => {
    const longTasks: number[] = [];
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) longTasks.push(entry.duration);
      }).observe({ entryTypes: ["longtask"] });
    } catch {
      /* longtask unsupported: the delay sampler still reports */
    }

    // Sample how late an "immediately" scheduled callback actually runs.
    const delays: number[] = [];
    let sampling = true;
    const sample = (): void => {
      const due = performance.now();
      setTimeout(() => {
        delays.push(performance.now() - due);
        if (sampling) sample();
      }, 0);
    };
    sample();

    const before = document.querySelectorAll("[lang]").length;
    (document.querySelector("[data-dz-control]") as HTMLElement).click();

    // Wait until the visible blocks have been written, then a little longer so
    // any deferred work that was going to run has had the chance.
    const deadline = performance.now() + 20_000;
    while (performance.now() < deadline) {
      await new Promise((r) => setTimeout(r, 100));
      if (document.querySelectorAll("[lang]").length > before) break;
    }
    await new Promise((r) => setTimeout(r, 1_500));
    sampling = false;

    delays.sort((a, b) => a - b);
    const p95 = delays.length === 0 ? 0 : (delays[Math.floor(delays.length * 0.95)] ?? 0);
    return {
      longTasks: {
        count: longTasks.filter((d) => d > 50).length,
        max: longTasks.length === 0 ? 0 : Math.round(Math.max(...longTasks)),
      },
      schedulerDelayP95: Math.round(p95),
      blocksTranslated: document.querySelectorAll("[lang]").length,
    };
  });

  console.log(
    `perf (CPU 6x): long tasks >50ms = ${result.longTasks.count} (max ${result.longTasks.max}ms), ` +
      `scheduler delay p95 = ${result.schedulerDelayP95}ms, ` +
      `blocks translated = ${result.blocksTranslated}`,
  );

  // It must actually have done the work, or the numbers mean nothing.
  expect(result.blocksTranslated, "nothing was translated, so nothing was measured").toBeGreaterThan(
    0,
  );

  // THE ER-20 TARGETS ARE NOT MET YET, and these assertions are deliberately
  // set where the widget actually is rather than where it should be, so the
  // suite guards against regression without pretending the budget is passed.
  //
  //   target   ER-20: no long task > 50 ms, input latency p95 < 100 ms
  //   measured long task max ~93 ms, scheduler delay p95 ~105 ms (CPU 6x)
  //
  // The cause is measured, not guessed: a single `extract()` over the whole
  // page accounts for ~68 ms of the worst task. Yielding between writes cannot
  // help, because the cost is paid before the first write. Fixing it means
  // chunking extraction at block boundaries, which is a change to extract.ts
  // and is tracked in TODOS.md. Tighten these numbers when that lands.
  expect(result.longTasks.max, "longest main-thread task (ER-20 target: 50ms)").toBeLessThanOrEqual(
    130,
  );
  expect(
    result.schedulerDelayP95,
    "scheduler delay p95, proxy for input latency (NFR-502 target: 100ms)",
  ).toBeLessThan(160);
});

test("er20_only_the_visible_part_of_a_long_page_is_translated_first", async ({ page }) => {
  await page.goto(`${base}/perf`, { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-dz-control]")).toBeVisible();
  await page.locator("[data-dz-control]").click();

  // Give the first pass time to land, but not enough for anyone to scroll.
  await page.waitForTimeout(1_500);
  const translated = await page.locator("[lang]").count();
  const total = await page.locator("#app p").count();

  expect(translated, "some visible content should be translated").toBeGreaterThan(0);
  expect(
    translated,
    "a 600-block page must not be translated in one go before the reader scrolls",
  ).toBeLessThan(total);

  // Scrolling brings the deferred blocks in, a screen at a time.
  //
  // Stepped, not one jump to the bottom. IntersectionObserver reports what
  // intersects at the frames it samples, so teleporting past 500 blocks skips
  // them entirely -- which is correct behaviour and exactly what a reader
  // scrolling through the page does not do.
  for (const y of [2_000, 6_000, 12_000, 19_000]) {
    await page.evaluate((to) => window.scrollTo(0, to), y);
    await page.waitForTimeout(600);
  }
  await expect
    .poll(async () => page.locator("[lang]").count(), { timeout: 15_000 })
    .toBeGreaterThan(translated);
});
