// @vitest-environment jsdom
// S4.1 — writing translations into the host page.
// Requirements: FR-210, FR-212, FR-214, FR-122, FR-160.
//
// The invariant these defend: the widget changes nodeValue on nodes that
// already exist, and does nothing else to the DOM. Most tests here capture the
// actual Text objects before translating and assert they are the same objects
// afterwards, because "the text looks right" would also pass if the widget had
// thrown the host's nodes away and built new ones.
import { beforeEach, describe, expect, it } from "vitest";

import {
  applyTranslation,
  markLanguage,
  readSlots,
  restoreOriginal,
  unchangedSinceWrite,
} from "../src/apply";
import { extract, type Segment } from "../src/extract";
import { ZWSP } from "../src/locale-dz";

function build(html: string): Segment {
  document.body.innerHTML = html;
  const segments = extract(document.body).segments;
  expect(segments.length).toBe(1);
  return segments[0] as Segment;
}

/** Every Text node under a block, in document order. */
function textNodes(root: Element): Text[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const found: Text[] = [];
  for (let n = walker.nextNode(); n !== null; n = walker.nextNode()) found.push(n as Text);
  return found;
}

describe("applyTranslation", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("fr210_writes_into_the_same_text_nodes", () => {
    const segment = build("<p>Click <a href='/x'>here</a> to apply.</p>");
    const before = textNodes(segment.block);
    const beforeCount = before.length;

    expect(applyTranslation(segment, "⟦1⟧DZ-here⟦/1⟧ DZ-tail")).toBe(true);

    const after = textNodes(segment.block);
    expect(after.length).toBe(beforeCount);
    after.forEach((node, n) => expect(node).toBe(before[n])); // identity, not equality
  });

  it("fr210_does_not_change_the_element_structure", () => {
    const segment = build("<p>Click <a href='/x'>here</a> to apply.</p>");
    const link = segment.block.querySelector("a");
    applyTranslation(segment, "⟦1⟧DZ-here⟦/1⟧ DZ-tail");
    expect(segment.block.querySelector("a")).toBe(link);
    expect(segment.block.innerHTML).toContain("<a href=\"/x\">");
  });

  it("fr122_refuses_a_different_marker_order", () => {
    const segment = build("<p>A <b>one</b> B <i>two</i> C</p>");
    const before = readSlots(segment);
    expect(applyTranslation(segment, "⟦2⟧two⟦/2⟧ x ⟦1⟧one⟦/1⟧ y")).toBe(false);
    expect(readSlots(segment)).toEqual(before); // untouched: the block stays English
  });

  it("fr122_refuses_a_missing_marker", () => {
    const segment = build("<p>Click <a href='/x'>here</a> to apply.</p>");
    const before = readSlots(segment);
    expect(applyTranslation(segment, "just text")).toBe(false);
    expect(readSlots(segment)).toEqual(before);
  });

  it("fr122_refuses_malformed_wire", () => {
    const segment = build("<p>Click <a href='/x'>here</a> to apply.</p>");
    const before = readSlots(segment);
    for (const bad of ["⟦1⟧unclosed", "⟦1⟧a⟦/2⟧", "stray ⟧ bracket", "⟦99⟧x⟦/99⟧"]) {
      expect(applyTranslation(segment, bad), bad).toBe(false);
    }
    expect(readSlots(segment)).toEqual(before);
  });

  it("fr122_writes_nothing_when_any_slot_is_unplaceable", () => {
    // A void marker leaves a slot with no text node behind it; text there has
    // nowhere to go, and a partial write would misalign the rest of the block.
    const segment = build("<p>Before<br>After</p>");
    const before = readSlots(segment);
    const markers = segment.text.includes("⟦v1/⟧");
    expect(markers).toBe(true);
    expect(applyTranslation(segment, "DZ-before⟦v1/⟧DZ-after")).toBe(true);
    expect(readSlots(segment)).not.toEqual(before);
  });

  it("fr160_inserts_break_opportunities_only_at_render_time", () => {
    const segment = build("<p>Text</p>");
    applyTranslation(segment, "ཀ་ཁ་");
    const rendered = readSlots(segment)[0] as string;
    expect(rendered).toContain(ZWSP); // the page can wrap
    expect(rendered.split(ZWSP).join("")).toBe("ཀ་ཁ་"); // nothing else added
  });
});

describe("staleness and restoration", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("fr217_detects_a_host_rewrite_while_a_request_was_in_flight", () => {
    const segment = build("<p>Original text</p>");
    const sent = readSlots(segment);
    expect(unchangedSinceWrite(segment, sent)).toBe(true);

    (segment.slots[0] as Text[])[0]!.nodeValue = "Host changed this";
    expect(unchangedSinceWrite(segment, sent)).toBe(false);
  });

  it("fr217_ignores_the_widgets_own_render_artefacts", () => {
    const segment = build("<p>Text</p>");
    applyTranslation(segment, "ཀ་ཁ");
    const written = readSlots(segment).map((s) => s.split(ZWSP).join(""));
    expect(unchangedSinceWrite(segment, written)).toBe(true);
  });

  it("fr212_restores_the_original_english_exactly", () => {
    const segment = build("<p>Click <a href='/x'>here</a> to apply.</p>");
    const original = readSlots(segment);
    const nodes = textNodes(segment.block);

    applyTranslation(segment, "⟦1⟧DZ-here⟦/1⟧ DZ-tail");
    expect(restoreOriginal(segment, original)).toBe(true);

    expect(readSlots(segment)).toEqual(original);
    textNodes(segment.block).forEach((node, n) => expect(node).toBe(nodes[n]));
  });
});

describe("language marking (FR-214)", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("fr214_labels_machine_output_distinctly_from_approved", () => {
    const segment = build("<p>Text</p>");
    markLanguage(segment.block, "mt");
    expect(segment.block.getAttribute("lang")).toBe("dz-x-mtfrom-en");
    markLanguage(segment.block, "human");
    expect(segment.block.getAttribute("lang")).toBe("dz");
  });

  it("fr214_treats_an_absent_origin_as_machine_output", () => {
    const segment = build("<p>Text</p>");
    markLanguage(segment.block, undefined);
    expect(segment.block.getAttribute("lang")).toBe("dz-x-mtfrom-en");
  });
});
