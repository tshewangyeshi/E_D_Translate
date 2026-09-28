// @vitest-environment jsdom
// S1.1 — widget block extraction and encoding.
// Requirements: FR-110, FR-111, FR-112, FR-113, FR-120, FR-121, FR-500, FR-512, NFR-304.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { extract, slotTexts, type ExtractOptions } from "../src/extract";

interface FixtureSegment {
  text: string;
  slots: string[];
  slotHasNode: boolean[];
  tierHint: number | null;
  selectorTier: number | null;
}
interface FixtureCase {
  name: string;
  requirements: string[];
  html: string;
  options?: ExtractOptions;
  expected: { segments: FixtureSegment[]; attributes: { attr: string; text: string }[] };
}

// vitest runs from adapters/widget; jsdom replaces import.meta.url, so resolve from cwd.
const fixturePath = resolve(process.cwd(), "../../tests/fixtures/extraction/cases.json");
const cases = (JSON.parse(readFileSync(fixturePath, "utf8")) as { cases: FixtureCase[] }).cases;

function load(html: string): HTMLElement {
  document.body.innerHTML = html;
  return document.body;
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("shared extraction fixtures (ER-O6)", () => {
  it.each(cases.map((c) => [`${c.requirements.join(" ")} ${c.name}`, c] as const))("%s", (_title, c) => {
    const { segments, attributes } = extract(load(c.html), c.options);
    expect(
      segments.map((s) => ({
        text: s.text,
        slots: slotTexts(s),
        slotHasNode: s.slots.map((nodes) => nodes.length > 0),
        tierHint: s.tierHint,
        selectorTier: s.selectorTier,
      })),
    ).toEqual(c.expected.segments);
    expect(attributes.map((a) => ({ attr: a.attr, text: a.text }))).toEqual(c.expected.attributes);
  });
});

describe("FR-120 FR-121 slots map to the host's own text nodes", () => {
  it("FR-121 slots hold the exact Text objects from the page, so writes happen in place (FR-210)", () => {
    const body = load("<p>Click <a href='/apply'>here</a> to apply.</p>");
    const p = body.querySelector("p")!;
    const [seg] = extract(body).segments;
    expect(seg!.block).toBe(p);
    expect(seg!.slots[0]![0]).toBe(p.childNodes[0]);
    expect(seg!.slots[1]![0]).toBe(p.querySelector("a")!.firstChild);
    expect(seg!.slots[2]![0]).toBe(p.childNodes[2]);
  });

  it("FR-121 adjacent text nodes (split by a host framework) share one slot", () => {
    const body = load("<p></p>");
    const p = body.querySelector("p")!;
    p.append(document.createTextNode("Apply "), document.createTextNode("before the deadline"));
    const [seg] = extract(body).segments;
    expect(seg!.text).toBe("Apply before the deadline");
    expect(seg!.slots).toHaveLength(1);
    expect(seg!.slots[0]).toHaveLength(2);
  });

  it("FR-121 nested inline text nodes collapse into the outermost marker's slot", () => {
    const body = load("<p>Pay <a>the <b>fee</b> online</a> today</p>");
    const [seg] = extract(body).segments;
    expect(seg!.text).toBe("Pay ⟦1⟧the fee online⟦/1⟧ today");
    expect(seg!.slots[1]).toHaveLength(3);
  });

  it("FR-120 an inline element wrapping block content is treated as a block", () => {
    const body = load("<div>Intro <a href='/x'><div>Card title</div></a> outro</div>");
    const texts = extract(body).segments.map((s) => s.text);
    expect(texts).toEqual(["Intro ", "Card title", " outro"]);
  });
});

describe("FR-112 extraction is order-stable", () => {
  it("FR-112 the same document yields the same segment sequence every time", () => {
    const html = cases.map((c) => c.html).join("");
    const first = extract(load(html)).segments.map((s) => s.text);
    const second = extract(load(html)).segments.map((s) => s.text);
    expect(second).toEqual(first);
    expect(first.length).toBeGreaterThan(5);
  });
});

describe("NFR-304 FR-111 exclusions never throw on bad configuration", () => {
  it("NFR-304 a malformed selector from config is ignored, not thrown (NFR-410)", () => {
    const body = load("<p>Translate me</p>");
    const opts: ExtractOptions = { privateSelectors: ["[[[broken"], tier1Selectors: [":::nope"] };
    expect(() => extract(body, opts)).not.toThrow();
    expect(extract(body, opts).segments.map((s) => s.text)).toEqual(["Translate me"]);
  });

  it("FR-111 an excluded root yields nothing", () => {
    const body = load("<p>Hidden</p>");
    body.setAttribute("data-dz-skip", "");
    expect(extract(body)).toEqual({ segments: [], attributes: [] });
    body.removeAttribute("data-dz-skip");
  });

  it("FR-111 text inside a hidden element is not extracted", () => {
    const body = load("<p hidden>Secret</p><p>Visible</p>");
    expect(extract(body).segments.map((s) => s.text)).toEqual(["Visible"]);
  });
});
