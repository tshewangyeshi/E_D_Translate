// @vitest-environment jsdom
// S4.3 — toggle and persistence.
// Requirements: FR-212, FR-213 · [ER-11, ER-19]
//
// Toggling back must give the reader the page as the host has it now: the
// original text byte for byte, including the whitespace a reader never sees
// but a layout depends on, and the host's own later edits rather than the
// widget's stale copy. And a reader who toggles all afternoon on an SPA must
// not leave the widget holding more and more.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RefList, Widget } from "../src/widget";
import type { ApiOptions } from "../src/api";

const CONFIG = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };

/** An API that translates everything: "DZ " before the text, markers kept. */
function echo(): ApiOptions {
  const impl = (async (url: string, init?: RequestInit) => {
    if (url.includes("/v1/config")) return { ok: true, status: 200, json: async () => CONFIG } as Response;
    const body = JSON.parse(String(init?.body));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        segments: body.segments.map((s: { id: string; text: string }) => ({
          id: s.id,
          text: `DZ ${s.text}`,
          status: "translated",
          origin: "mt",
        })),
      }),
    } as Response;
  }) as unknown as typeof fetch;
  return { base: "", site: "portal", fetchImpl: impl };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 15));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

let widget: Widget | null = null;

beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  widget?.unwatch();
  widget = null;
});

async function started(): Promise<Widget> {
  widget = new Widget(echo(), () => document.body);
  await widget.start();
  return widget;
}

describe("toggle restores exactly (FR-212)", () => {
  it("fr212_restores_the_original_whitespace_byte_for_byte", async () => {
    document.body.innerHTML =
      "<p>\n    Apply  for a passport\t online.\n  </p>" +
      '<p>  Click <a href="/apply"> here </a> to apply. </p>' +
      "<ul>\n  <li> First step </li>\n  <li>Second step</li>\n</ul>";
    const before = document.body.innerHTML;
    const nodes = [...document.querySelectorAll("p, a, li")].map((e) => e.firstChild);
    const w = await started();

    await w.translate();
    expect(document.body.textContent).toContain("DZ");
    w.toggleBack();

    expect(document.body.innerHTML).toBe(before);
    expect([...document.querySelectorAll("p, a, li")].map((e) => e.firstChild)).toEqual(nodes);
  });

  it("fr212_regression_a_fee_changed_while_dzongkha_was_on_reads_the_new_fee", async () => {
    document.body.innerHTML = "<p>The renewal fee is Nu. 500.</p>";
    const w = await started();
    await w.translate();
    expect(document.body.textContent).toBe("DZ The renewal fee is Nu. 500.");

    // The host updates the fee while the reader is looking at Dzongkha.
    document.querySelector("p")!.firstChild!.nodeValue = "The renewal fee is Nu. 600.";
    w.toggleBack();

    expect(document.body.textContent).toBe("The renewal fee is Nu. 600.");
  });

  it("fr212_regression_holds_when_the_widget_retranslates_the_host_change", async () => {
    document.body.innerHTML = "<p>The renewal fee is Nu. 500.</p>";
    const w = await started();
    await w.translate();
    w.watch(1);

    document.querySelector("p")!.firstChild!.nodeValue = "The renewal fee is Nu. 600.";
    await settle();
    expect(document.body.textContent).toBe("DZ The renewal fee is Nu. 600."); // FR-211
    w.toggleBack();

    expect(document.body.textContent).toBe("The renewal fee is Nu. 600.");
  });
});

describe("repeated toggling holds nothing back (ER-19)", () => {
  it(
    "er19_a_thousand_blocks_fifty_cycles_leave_no_state",
    async () => {
      const w = await started();
      const host = document.createElement("div");
      document.body.appendChild(host);
      for (let cycle = 0; cycle < 50; cycle += 1) {
        for (let n = 0; n < 1000; n += 1) {
          const p = document.createElement("p");
          p.textContent = `Block ${n} of cycle ${cycle}.`;
          host.appendChild(p);
        }
        await w.translate();
        expect(host.firstElementChild?.textContent).toBe(`DZ Block 0 of cycle ${cycle}.`);
        w.toggleBack();
        expect(host.firstElementChild?.textContent).toBe(`Block 0 of cycle ${cycle}.`);
        host.replaceChildren(); // the route changes: every block goes
        expect(w.stateSize()).toEqual({ translatedBlocks: 0, translatedAttributes: 0 });
      }
    },
    120_000,
  );

  it("er19_a_block_retranslated_after_host_changes_is_held_once", async () => {
    document.body.innerHTML = "<p>The renewal fee is Nu. 500.</p>";
    const w = await started();
    await w.translate();
    w.watch(1);
    for (const fee of [600, 700, 800]) {
      document.querySelector("p")!.firstChild!.nodeValue = `The renewal fee is Nu. ${fee}.`;
      await settle();
    }
    expect(document.body.textContent).toBe("DZ The renewal fee is Nu. 800.");
    expect(w.stateSize().translatedBlocks).toBe(1);
  });
});

describe("the held list drops what was collected (ER-19)", () => {
  const collected = new WeakSet<object>();

  beforeEach(() => {
    // A WeakRef whose target is "collected" when the test says so.
    class TestRef<T extends object> {
      constructor(private readonly target: T) {}
      deref(): T | undefined {
        return collected.has(this.target) ? undefined : this.target;
      }
    }
    vi.stubGlobal("WeakRef", TestRef);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("er19_stays_proportional_to_what_is_alive", () => {
    const list = new RefList<object>();
    for (let route = 0; route < 50; route += 1) {
      const page = Array.from({ length: 100 }, () => ({}));
      for (const block of page) list.add(block);
      for (const block of page) collected.add(block); // the route changed; GC ran
    }
    expect(list.size).toBeLessThan(200); // without compaction: 5,000
    expect([...list.live()]).toEqual([]);
  });

  it("er19_keeps_a_detached_element_that_is_still_alive", () => {
    const list = new RefList<object>();
    const keptAlive = {};
    list.add(keptAlive);
    for (let n = 0; n < 500; n += 1) {
      const gone = {};
      list.add(gone);
      collected.add(gone);
    }
    expect([...list.live()]).toEqual([keptAlive]);
  });
});
