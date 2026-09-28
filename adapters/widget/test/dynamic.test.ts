// @vitest-environment jsdom
// S4.2 — content that arrives or changes after load, and attributes.
// Requirements: FR-113, FR-211, FR-212 · [ER-11, ER-18, ER-20]
//
// The trap this story exists to avoid: the widget writes to the page, the
// observer sees the write, the widget translates its own translation. A
// re-entrancy flag looks like the fix and is not, because MutationObserver
// delivers records asynchronously -- the flag is down again by the time they
// arrive. These tests pin the value-comparison approach instead, including the
// case where the host and the widget write to the very same node.
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Widget } from "../src/widget";
import { observe } from "../src/observe";
import type { ApiOptions } from "../src/api";

const CONFIG = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };

/** Counts requests so "did it translate again?" is answerable. */
function api(): { options: ApiOptions; texts: string[][] } {
  const texts: string[][] = [];
  const impl = (async (url: string, init?: RequestInit) => {
    if (url.includes("/v1/config")) {
      return { ok: true, status: 200, json: async () => CONFIG } as Response;
    }
    const body = JSON.parse(String(init?.body));
    texts.push(body.segments.map((s: { text: string }) => s.text));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        segments: body.segments.map((s: { id: string; text: string }) => ({
          id: s.id,
          text: `DZ:${s.text}`,
          status: "translated",
          origin: "mt",
        })),
      }),
    } as Response;
  }) as unknown as typeof fetch;
  return { options: { base: "", site: "portal", fetchImpl: impl }, texts };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("observer mechanics (FR-211)", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("fr211_reports_added_content", async () => {
    document.body.innerHTML = "<div id='host'></div>";
    const seen: Element[] = [];
    const watcher = observe({
      root: document.body,
      blockOf: (node) => (node.nodeType === 1 ? (node as Element) : node.parentElement),
      isOwnWrite: () => false,
      onDirty: (blocks) => seen.push(...blocks),
      debounceMs: 5,
    });
    document.getElementById("host")!.innerHTML = "<p>Added later.</p>";
    await new Promise((r) => setTimeout(r, 20));
    expect(seen.length).toBeGreaterThan(0);
    watcher.stop();
  });

  it("fr211_watches_characterdata_not_just_childlist", async () => {
    // A framework re-render changes a value in place. Watching only childList
    // would make every React and Vue update invisible.
    document.body.innerHTML = "<p>Original</p>";
    const node = document.querySelector("p")!.firstChild as Text;
    let reported = 0;
    const watcher = observe({
      root: document.body,
      blockOf: (n) => (n.nodeType === 1 ? (n as Element) : n.parentElement),
      isOwnWrite: () => false,
      onDirty: () => (reported += 1),
      debounceMs: 5,
    });
    node.nodeValue = "Changed in place";
    await new Promise((r) => setTimeout(r, 20));
    expect(reported).toBe(1);
    watcher.stop();
  });

  it("er11_ignores_the_widgets_own_writes", async () => {
    document.body.innerHTML = "<p>Original</p>";
    const node = document.querySelector("p")!.firstChild as Text;
    let reported = 0;
    const watcher = observe({
      root: document.body,
      blockOf: (n) => (n.nodeType === 1 ? (n as Element) : n.parentElement),
      isOwnWrite: () => true, // everything looks like our own write
      onDirty: () => (reported += 1),
      debounceMs: 5,
    });
    node.nodeValue = "Widget wrote this";
    await new Promise((r) => setTimeout(r, 20));
    expect(reported).toBe(0);
    watcher.stop();
  });

  it("er20_a_burst_of_changes_is_one_batch_not_hundreds", async () => {
    document.body.innerHTML = "<div id='host'></div>";
    const batches: number[] = [];
    const watcher = observe({
      root: document.body,
      blockOf: (n) => (n.nodeType === 1 ? (n as Element) : n.parentElement),
      isOwnWrite: () => false,
      onDirty: (blocks) => batches.push(blocks.size),
      debounceMs: 10,
    });
    // An SPA route change: many mutations in quick succession.
    const host = document.getElementById("host")!;
    for (let n = 0; n < 200; n += 1) {
      const p = document.createElement("p");
      p.textContent = `Line ${n}`;
      host.appendChild(p);
    }
    await new Promise((r) => setTimeout(r, 40));
    expect(batches.length).toBe(1);
    watcher.stop();
  });
});

describe("dynamic content end to end (FR-211, FR-212)", () => {
  beforeEach(() => {
    document.body.innerHTML = "<div id='host'><p>First block.</p></div>";
  });

  it("fr211_translates_content_added_after_the_first_pass", async () => {
    const { options, texts } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();
    widget.watch(1);
    expect(document.querySelector("p")?.textContent).toBe("DZ:First block.");

    const added = document.createElement("p");
    added.textContent = "Arrived later.";
    document.getElementById("host")!.appendChild(added);

    await new Promise((r) => setTimeout(r, 10));
    await settle();
    expect(added.textContent).toBe("DZ:Arrived later.");
    expect(texts.length).toBe(2); // one initial pass, one for the new content
    widget.unwatch();
  });

  it("er11_does_not_translate_its_own_translation", async () => {
    const { options, texts } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();
    widget.watch(1);

    await new Promise((r) => setTimeout(r, 15));
    await settle();

    // The only write was the widget's own, so nothing more should have been sent.
    expect(texts.length).toBe(1);
    expect(document.querySelector("p")?.textContent).toBe("DZ:First block.");
    widget.unwatch();
  });

  it("fr212_a_host_edit_becomes_the_new_original", async () => {
    const { options } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();
    widget.watch(1);

    // The host rewrites the block it owns, in place, as a framework would.
    (document.querySelector("p")!.firstChild as Text).nodeValue = "Host wrote new English.";
    await new Promise((r) => setTimeout(r, 10));
    await settle();

    expect(document.querySelector("p")?.textContent).toBe("DZ:Host wrote new English.");

    widget.toggleBack();
    expect(document.querySelector("p")?.textContent).toBe("Host wrote new English.");
    widget.unwatch();
  });
});

describe("attributes (FR-113)", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <p>Body text.</p>
      <img src="x.png" alt="A passport photo">
      <input placeholder="Enter your name" aria-label="Name field">
      <button title="Submit form" aria-description="does the thing">Go</button>`;
  });

  it("fr113_translates_alt_title_and_placeholder", async () => {
    const { options } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();

    expect(document.querySelector("img")?.getAttribute("alt")).toBe("DZ:A passport photo");
    expect(document.querySelector("input")?.getAttribute("placeholder")).toBe("DZ:Enter your name");
    expect(document.querySelector("button")?.getAttribute("title")).toBe("DZ:Submit form");
  });

  it("fr113_never_touches_aria_labels", async () => {
    // No screen reader ships a Dzongkha voice, so a translated aria-label would
    // be read out in English phonetics and make the page worse, not better.
    const { options } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();

    expect(document.querySelector("input")?.getAttribute("aria-label")).toBe("Name field");
    expect(document.querySelector("button")?.getAttribute("aria-description")).toBe(
      "does the thing",
    );
  });

  it("fr113_restores_attributes_exactly_on_toggle_back", async () => {
    const { options } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();
    widget.toggleBack();

    expect(document.querySelector("img")?.getAttribute("alt")).toBe("A passport photo");
    expect(document.querySelector("input")?.getAttribute("placeholder")).toBe("Enter your name");
    expect(document.querySelector("button")?.getAttribute("title")).toBe("Submit form");
  });

  it("fr113_leaves_a_host_changed_attribute_alone", async () => {
    const { options } = api();
    const widget = new Widget(options, () => document.body);
    await widget.start();
    await widget.translate();

    document.querySelector("img")!.setAttribute("alt", "Host changed this");
    widget.toggleBack();
    expect(document.querySelector("img")?.getAttribute("alt")).toBe("Host changed this");
  });
});
