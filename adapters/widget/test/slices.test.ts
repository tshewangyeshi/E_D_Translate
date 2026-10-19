// @vitest-environment jsdom
// S4.2 / ER-20 — extraction that hands the main thread back.
// Requirements: FR-110, FR-211, FR-217, NFR-502.
//
// A long page extracted in one pass held the main thread for ~68 ms (CPU 6x),
// and a tap in that window does nothing. Extraction now pauses between slices.
// The pauses must change nothing but timing: the same segments, every block
// read in one go, and no answer written over text the host changed while the
// widget was paused.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { extract, extractInSlices, slotTexts, type ExtractOptions } from "../src/extract";
import { Widget } from "../src/widget";

interface FixtureCase {
  name: string;
  html: string;
  options?: ExtractOptions;
}

const fixturePath = resolve(process.cwd(), "../../tests/fixtures/extraction/cases.json");
const cases = (JSON.parse(readFileSync(fixturePath, "utf8")) as { cases: FixtureCase[] }).cases;

/** A clock that moves `step` ms every time it is read: every boundary is over budget. */
function racingClock(step = 10): () => number {
  let t = 0;
  return () => (t += step);
}

function shape(found: ReturnType<typeof extract>): unknown {
  return {
    segments: found.segments.map((s) => ({
      text: s.text,
      slots: slotTexts(s),
      read: s.read,
      tierHint: s.tierHint,
      selectorTier: s.selectorTier,
    })),
    attributes: found.attributes.map((a) => ({ attr: a.attr, text: a.text })),
  };
}

function longPage(blocks: number): string {
  return Array.from({ length: blocks }, (_, n) => `<p>Paragraph ${n} about <b>passports</b>.</p>`).join(
    "",
  );
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("pausing changes nothing but timing (ER-20)", () => {
  it.each(cases.map((c) => [c.name, c] as const))(
    "er20_pausing_at_every_boundary_gives_the_same_extraction: %s",
    async (_name, c) => {
      document.body.innerHTML = c.html;
      const whole = shape(extract(document.body, c.options));
      document.body.innerHTML = c.html;
      const sliced = await extractInSlices(document.body, c.options ?? {}, async () => {}, 0);
      expect(shape(sliced)).toEqual(whole);
    },
  );

  it("er20_a_long_page_is_extracted_in_several_slices", async () => {
    document.body.innerHTML = longPage(200);
    let pauses = 0;
    const found = await extractInSlices(
      document.body,
      {},
      async () => {
        pauses++;
      },
      8,
      racingClock(),
    );
    expect(found.segments).toHaveLength(200);
    expect(pauses).toBeGreaterThan(100); // a block boundary each, and the attribute pass
  });

  it("er20_work_within_the_budget_does_not_pause", async () => {
    document.body.innerHTML = longPage(50);
    let pauses = 0;
    await extractInSlices(
      document.body,
      {},
      async () => {
        pauses++;
      },
      8,
      () => 0, // the clock never moves: never over budget
    );
    expect(pauses).toBe(0);
  });

  it("fr110_a_block_is_never_split_by_a_pause", async () => {
    // Every pause rewrites every paragraph. A pause inside a block would mix
    // its old and new text; at block boundaries each block is read whole.
    document.body.innerHTML = longPage(20);
    let round = 0;
    const found = await extractInSlices(
      document.body,
      {},
      async () => {
        round++;
        for (const b of Array.from(document.querySelectorAll("b"))) {
          (b.firstChild as Text).nodeValue = `round${round}`;
        }
      },
      0,
    );
    for (const segment of found.segments) {
      const sent = segment.text.replace(/⟦\/?\d+⟧/g, "");
      expect(sent).toBe(segment.read.join(""));
    }
  });
});

describe("the widget with pausing extraction (FR-217)", () => {
  const config = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };

  afterEach(() => {
    vi.restoreAllMocks();
    delete (window as unknown as { scheduler?: unknown }).scheduler;
  });

  it("fr217_a_block_the_host_changes_during_a_pause_keeps_the_hosts_text", async () => {
    document.body.innerHTML = "<p>First paragraph.</p><p>Second paragraph.</p>";
    const [first, second] = Array.from(document.querySelectorAll("p")) as HTMLElement[];
    vi.spyOn(performance, "now").mockImplementation(racingClock());

    // The second pause comes after the first paragraph was extracted and
    // before the second: the host rewrites the first one there.
    let pauses = 0;
    (window as unknown as { scheduler: unknown }).scheduler = {
      yield: async () => {
        pauses++;
        if (pauses === 2) first!.firstChild!.nodeValue = "Host replaced this";
      },
    };

    const sent: string[] = [];
    const impl = (async (url: string, init?: RequestInit) => {
      if (url.includes("/v1/config")) {
        return { ok: true, status: 200, json: async () => config } as Response;
      }
      const body = JSON.parse(String(init?.body)) as { segments: { id: string; text: string }[] };
      sent.push(...body.segments.map((s) => s.text));
      return {
        ok: true,
        status: 200,
        json: async () => ({
          segments: body.segments.map((s) => ({ id: s.id, text: `DZ ${s.text}`, status: "translated" })),
        }),
      } as Response;
    }) as unknown as typeof fetch;

    const widget = new Widget({ base: "https://api.example", site: "portal", fetchImpl: impl }, () =>
      document.body,
    );
    await widget.start();
    await widget.translate();

    expect(pauses).toBeGreaterThanOrEqual(2);
    expect(sent).toEqual(["First paragraph.", "Second paragraph."]);
    expect(first!.textContent).toBe("Host replaced this"); // the answer was about other text
    expect(second!.textContent).toBe("DZ Second paragraph.");
  });

  it("er20_switching_back_during_extraction_sends_nothing", async () => {
    document.body.innerHTML = longPage(5);
    vi.spyOn(performance, "now").mockImplementation(racingClock());
    let widget: Widget | null = null;
    (window as unknown as { scheduler: unknown }).scheduler = {
      yield: async () => {
        widget?.toggleBack();
      },
    };
    let translateCalls = 0;
    const impl = (async (url: string) => {
      if (url.includes("/v1/config")) {
        return { ok: true, status: 200, json: async () => config } as Response;
      }
      translateCalls++;
      return { ok: true, status: 200, json: async () => ({ segments: [] }) } as Response;
    }) as unknown as typeof fetch;

    widget = new Widget({ base: "https://api.example", site: "portal", fetchImpl: impl }, () =>
      document.body,
    );
    await widget.start();
    await widget.translate();
    expect(translateCalls).toBe(0);
  });
});
