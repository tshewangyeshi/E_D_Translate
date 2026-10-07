// @vitest-environment jsdom
// S6.1 — Dzongkha typography: scope and cleanup.
// Requirements: FR-340, FR-341, FR-210, FR-212
//
// The browser half (fonts, sizes, clipping, wrapping) is test/e2e/render.spec.ts.
// Here: the rules can only ever reach blocks the widget wrote, and toggling
// back leaves no trace of them.
import { describe, expect, it } from "vitest";

import {
  WRITTEN,
  clearWritten,
  installTypography,
  markWritten,
  originalSize,
  typographyCss,
} from "../src/typography";
import { Widget } from "../src/widget";
import type { ApiOptions } from "../src/api";

const CSS = typographyCss("https://cdn.example/fonts/dzweb-dzongkha.woff2");

describe("scope (FR-340)", () => {
  it("fr340_every_rule_targets_only_written_blocks", () => {
    // A host page in Dzongkha (the G2C portal is <html lang="dzo">) must not be restyled.
    expect(CSS).not.toMatch(/\[lang/);
    for (const rule of CSS.split("\n").filter((r) => !r.startsWith("@font-face") && !r.startsWith(":where(:root)"))) {
      expect(rule).toContain(`[${WRITTEN}]`);
    }
  });

  it("fr340_declares_the_self_hosted_font_for_tibetan_only_with_swap", () => {
    expect(CSS).toContain('src:url("https://cdn.example/fonts/dzweb-dzongkha.woff2") format("woff2")');
    expect(CSS).toContain("font-display:swap");
    expect(CSS).toContain("unicode-range:U+0F00-0FFF");
  });

  it("fr341_exposes_the_type_scale_as_custom_properties_a_host_can_override", () => {
    expect(CSS).toContain(":where(:root){--dzweb-dz-line-height:2;--dzweb-dz-scale:1.3;");
    expect(CSS).toContain("line-height:var(--dzweb-dz-line-height)!important");
    expect(CSS).toContain("var(--dzweb-dz-line-height)");
    expect(CSS).toContain("var(--dzweb-dz-scale)");
  });

  it("fr340_installs_one_style_block_however_often_it_is_asked", () => {
    document.head.innerHTML = "";
    installTypography(document, "/f.woff2");
    installTypography(document, "/f.woff2");
    expect(document.querySelectorAll("style[data-dz-style]")).toHaveLength(1);
  });
});

describe("marking and cleanup (FR-212)", () => {
  it("fr212_clearing_leaves_no_attribute_and_no_empty_style", () => {
    document.body.innerHTML = "<p>Text</p>";
    const p = document.querySelector("p")!;
    const before = p.outerHTML;
    markWritten(p, "16px");
    expect(p.hasAttribute(WRITTEN)).toBe(true);
    expect(p.style.getPropertyValue("--dz-base")).toBe("16px");
    clearWritten(p);
    expect(p.outerHTML).toBe(before);
  });

  it("fr212_a_host_style_survives_marking_and_clearing", () => {
    document.body.innerHTML = '<p style="color: red;">Text</p>';
    const p = document.querySelector("p")!;
    markWritten(p, "16px");
    clearWritten(p);
    expect(p.getAttribute("style")).toBe("color: red;");
  });

  it("fr341_a_block_keeps_the_size_it_was_first_measured_at", () => {
    document.body.innerHTML = "<p>Text</p>";
    const p = document.querySelector("p")!;
    markWritten(p, "18px");
    expect(originalSize(p)).toBe("18px"); // not its scaled size, if it is re-translated
  });
});

describe("the widget marks what it writes", () => {
  const config = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };
  const impl = (async (url: string, init?: RequestInit) => {
    if (url.includes("/v1/config")) return { ok: true, status: 200, json: async () => config } as Response;
    const body = JSON.parse(String(init?.body));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        segments: body.segments.map((s: { id: string; text: string }) => ({
          id: s.id,
          text: `DZ ${s.text}`,
          status: s.text.startsWith("Legal") ? "tier_blocked" : "translated",
          origin: "mt",
        })),
      }),
    } as Response;
  }) as unknown as typeof fetch;
  const api: ApiOptions = { base: "", site: "portal", fetchImpl: impl };

  it("fr340_written_blocks_are_marked_refused_ones_are_not_and_toggling_back_clears_all", async () => {
    document.body.innerHTML = "<p>Apply online.</p><p>Legal notice.</p>";
    const before = document.body.innerHTML;
    let installs = 0;
    const widget = new Widget(api, () => document.body);
    widget.beforeWrite = () => {
      installs += 1;
    };
    await widget.start();
    await widget.translate();

    const written = document.querySelectorAll("p")[0]!;
    const refused = document.querySelectorAll("p")[1]!;
    expect(written.hasAttribute(WRITTEN)).toBe(true);
    expect(refused.hasAttribute(WRITTEN)).toBe(false); // English keeps host typography
    expect(installs).toBeGreaterThan(0);

    widget.toggleBack();
    expect(document.body.innerHTML).toBe(before);
  });
});

describe("English inside Dzongkha (S6.1)", () => {
  const config = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };
  const impl = (async (url: string, init?: RequestInit) => {
    if (url.includes("/v1/config")) return { ok: true, status: 200, json: async () => config } as Response;
    const body = JSON.parse(String(init?.body));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        segments: body.segments.map((s: { id: string; text: string }) => ({
          id: s.id,
          text: `DZ ${s.text}`,
          status: s.text.includes("non-refundable") ? "tier_blocked" : "translated",
          origin: "mt",
        })),
      }),
    } as Response;
  }) as unknown as typeof fetch;

  it("s61_a_refused_block_inside_a_written_one_is_marked_and_cleared", async () => {
    document.body.innerHTML = "<div>Renew online.<p>Fees are non-refundable.</p></div>";
    const before = document.body.innerHTML;
    const widget = new Widget({ base: "", site: "portal", fetchImpl: impl }, () => document.body);
    widget.beforeWrite = () => undefined; // typography on
    await widget.start();
    await widget.translate();

    const outer = document.querySelector("div")!;
    const inner = document.querySelector("p")!;
    expect(outer.hasAttribute(WRITTEN)).toBe(true);
    expect(inner.hasAttribute("data-dz-english")).toBe(true);
    expect(inner.textContent).toBe("Fees are non-refundable.");

    widget.toggleBack();
    expect(document.body.innerHTML).toBe(before);
  });
});
