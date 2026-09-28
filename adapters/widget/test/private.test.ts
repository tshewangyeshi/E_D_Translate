// @vitest-environment jsdom
// S3.4 — pages and regions the site marks as carrying personal data.
// Requirements: NFR-304.
//
// Two strengths of refusal, and the difference matters. A `private_selectors`
// region or `data-dz-skip` element is not extracted: the widget still runs on
// the page. `data-dz-private` on the page itself is stronger -- the widget does
// not load, offers no control, and never reads the page at all.
//
// The stronger form exists because a citizen's own application form should not
// be walked by a translation service even to conclude there is nothing to send.
import { beforeEach, describe, expect, it, vi } from "vitest";

import { boot } from "../src/main";
import { extract } from "../src/extract";

const CONFIG = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };

function stubFetch(): { calls: string[] } {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (url: string) => {
    calls.push(url);
    return { ok: true, status: 200, json: async () => CONFIG } as Response;
  });
  return { calls };
}

describe("data-dz-private on the page (NFR-304)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.removeAttribute("data-dz-private");
    document.body.removeAttribute("data-dz-private");
    document.body.innerHTML = "";
  });

  it("nfr304_does_not_load_on_a_page_marked_private", async () => {
    document.documentElement.setAttribute("data-dz-private", "");
    document.body.innerHTML = `<script data-dz-site="portal"></script><p>Citizen record.</p>`;
    const { calls } = stubFetch();

    expect(await boot()).toBe(null);
    expect(calls, "configuration was requested for a private page").toEqual([]);
    expect(document.querySelector("[data-dz-control]"), "a control was offered").toBe(null);
  });

  it("nfr304_honours_the_marker_on_the_body_too", async () => {
    // Templates wrapping signed-in pages own one or the other.
    document.body.setAttribute("data-dz-private", "");
    document.body.innerHTML += `<script data-dz-site="portal"></script>`;
    const { calls } = stubFetch();

    expect(await boot()).toBe(null);
    expect(calls).toEqual([]);
  });

  it("nfr304_loads_normally_on_a_page_without_the_marker", async () => {
    document.body.innerHTML = `<script data-dz-site="portal"></script><p>Public page.</p>`;
    const { calls } = stubFetch();

    const widget = await boot();
    expect(widget).not.toBe(null);
    expect(calls.some((url) => url.includes("/v1/config"))).toBe(true);
    expect(document.querySelector("[data-dz-control]")).not.toBe(null);
  });
});

describe("regions within a public page (NFR-304)", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("nfr304_never_extracts_a_configured_private_region", () => {
    document.body.innerHTML = `
      <p>Public notice.</p>
      <div class="profile"><p>Testperson Examplename, CID 00000000000</p></div>`;
    const texts = extract(document.body, { privateSelectors: [".profile"] }).segments.map(
      (s) => s.text,
    );
    expect(texts).toEqual(["Public notice."]);
  });

  it("nfr304_never_extracts_a_data_dz_skip_element", () => {
    document.body.innerHTML = `
      <p>Public notice.</p>
      <p data-dz-skip>Account balance: Nu. 4,210</p>`;
    const texts = extract(document.body).segments.map((s) => s.text);
    expect(texts).toEqual(["Public notice."]);
  });

  it("nfr304_private_regions_exclude_their_descendants", () => {
    document.body.innerHTML = `
      <div class="profile">
        <p>Name</p>
        <div><p>Nested personal detail</p></div>
      </div>`;
    const found = extract(document.body, { privateSelectors: [".profile"] });
    expect(found.segments).toEqual([]);
    expect(found.attributes).toEqual([]);
  });

  it("nfr304_private_regions_exclude_their_attributes", () => {
    document.body.innerHTML = `
      <div class="profile"><img src="x.png" alt="Photograph of Testperson Examplename"></div>`;
    const found = extract(document.body, { privateSelectors: [".profile"] });
    expect(found.attributes).toEqual([]);
  });
});
