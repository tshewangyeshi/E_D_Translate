// @vitest-environment jsdom
// S3.2 — the notice, wired to the widget that translates the page.
// Requirements: FR-520, FR-522, FR-430, FR-432.
//
// The notice and the widget each had their own tests and both passed while two
// things were wrong between them: the notice missed machine output that arrived
// after the first pass, and the widget translated the notice. So these tests
// start the widget the way a page does, with `boot()`, and look at the page.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sendFeedback, type ApiOptions } from "../src/api";
import { CLOSE_EN, NOTICE_EN, REPORT_EN } from "../src/locale-dz";
import { boot } from "../src/main";
import type { Widget } from "../src/widget";

const CONFIG = { site: "portal", default_tier: 2, tier1_selectors: [], private_selectors: [] };
const KEY = "a".repeat(64);

interface Sent {
  url: string;
  body: { segments?: { id: string; text: string }[] } & Record<string, unknown>;
}

type Answer = (segment: { id: string; text: string }) => Record<string, unknown>;

const machine: Answer = (s) => ({
  id: s.id,
  text: `DZ:${s.text}`,
  status: "translated",
  origin: "mt",
  segment_key: KEY,
});
const approved: Answer = (s) => ({ ...machine(s), origin: "human" });
const pending: Answer = (s) => ({ id: s.id, text: s.text, status: "pending_mt" });

/** Answers /v1/translate with `answers[n]` for the n-th request, repeating the last. */
function server(...answers: Answer[]): Sent[] {
  const sent: Sent[] = [];
  let n = 0;
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (url.includes("/v1/config")) {
      return { ok: true, status: 200, json: async () => CONFIG } as Response;
    }
    sent.push({ url, body });
    if (url.includes("/v1/feedback")) return { ok: true, status: 202 } as Response;
    const answer = answers[Math.min(n++, answers.length - 1)]!;
    const segments = (body.segments as { id: string; text: string }[]).map(answer);
    return { ok: true, status: 200, json: async () => ({ segments }) } as Response;
  });
  return sent;
}

function translateRequests(sent: Sent[]): Sent[] {
  return sent.filter((s) => s.url.includes("/v1/translate"));
}

function everyTextSent(sent: Sent[]): string {
  return translateRequests(sent)
    .flatMap((s) => (s.body.segments ?? []).map((segment) => segment.text))
    .join("\n");
}

const notice = () => document.querySelector("[data-dz-notice]");
const toggle = () => document.querySelector("button[data-dz-control]");

// Every widget a test boots keeps observing the page; stop them all
// afterwards, or one test's widget reacts to the next test's DOM.
const started: Widget[] = [];

async function start(html: string): Promise<Widget> {
  document.body.innerHTML = `<script data-dz-site="portal" data-dz-api="https://api.example"></script>${html}`;
  const widget = await boot();
  expect(widget).not.toBe(null);
  started.push(widget!);
  return widget!;
}

async function switchToDzongkha(): Promise<void> {
  (toggle() as HTMLElement).click();
  await vi.waitFor(() => expect(toggle()?.textContent).toBe("English"));
}

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = "";
});

afterEach(() => {
  for (const widget of started.splice(0)) {
    widget.toggleBack();
    widget.unwatch();
  }
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the notice follows machine output (FR-520)", () => {
  it("fr520_appears_when_the_first_pass_writes_machine_output", async () => {
    server(machine);
    await start("<p>Apply for a passport.</p>");
    expect(notice()).toBe(null); // an English page needs no warning

    await switchToDzongkha();
    expect(document.querySelector("p")?.getAttribute("lang")).toBe("dz-x-mtfrom-en");
    expect(notice()).not.toBe(null);
  });

  it("fr520_appears_when_machine_output_only_arrives_on_the_retry", async () => {
    // The normal first visit: the server has not translated the page yet and
    // answers pending_mt, then has it ready when the widget asks again.
    vi.useFakeTimers();
    server(pending, machine);
    await start("<p>Apply for a passport.</p>");
    (toggle() as HTMLElement).click();
    await vi.advanceTimersByTimeAsync(100);
    expect(document.querySelector("p")?.hasAttribute("lang")).toBe(false);
    expect(notice()).toBe(null); // nothing machine-made on the page yet

    await vi.advanceTimersByTimeAsync(9_000);
    expect(document.querySelector("p")?.getAttribute("lang")).toBe("dz-x-mtfrom-en");
    expect(notice(), "machine output is on the page with no notice").not.toBe(null);
  });

  it("fr520_appears_for_content_the_host_adds_later", async () => {
    server(approved, machine);
    const widget = await start("<p>Apply for a passport.</p>");
    await switchToDzongkha();
    expect(notice()).toBe(null); // approved text needs no warning

    const added = document.createElement("p");
    added.textContent = "Offices close at five.";
    document.body.appendChild(added);
    widget.flush();
    await vi.waitFor(() => expect(added.getAttribute("lang")).toBe("dz-x-mtfrom-en"));
    expect(notice()).not.toBe(null);
  });

  it("fr520_appears_when_only_an_attribute_is_machine_translated", async () => {
    server((s) => (s.id.startsWith("a") ? machine(s) : approved(s)));
    await start('<p>Apply for a passport.</p><img alt="The immigration office" src="x.png">');
    await switchToDzongkha();
    expect(document.querySelector("img")?.getAttribute("alt")).toBe("DZ:The immigration office");
    expect(notice()).not.toBe(null);
  });

  it("fr520_stays_away_when_everything_on_the_page_is_approved", async () => {
    server(approved);
    const widget = await start("<p>Apply for a passport.</p>");
    await switchToDzongkha();
    expect(document.querySelector("p")?.getAttribute("lang")).toBe("dz");
    expect(widget.showingMachineOutput).toBe(false);
    expect(notice()).toBe(null);
  });

  it("fr520_goes_when_the_reader_returns_to_english", async () => {
    server(machine);
    const widget = await start("<p>Apply for a passport.</p>");
    await switchToDzongkha();
    expect(widget.showingMachineOutput).toBe(true);

    (toggle() as HTMLElement).click();
    await vi.waitFor(() => expect(notice()).toBe(null));
    expect(widget.showingMachineOutput).toBe(false);
    expect(document.querySelector("p")?.textContent).toBe("Apply for a passport.");
  });
});

describe("the widget leaves its own controls alone (FR-520, FR-521, FR-522)", () => {
  it("fr520_the_widget_never_translates_its_own_notice", async () => {
    vi.useFakeTimers();
    const sent = server(pending, machine);
    const widget = await start("<p>Apply for a passport.</p>");
    (toggle() as HTMLElement).click();
    await vi.advanceTimersByTimeAsync(9_000); // first pass, then the full-page retry
    expect(notice()).not.toBe(null);

    // Every way the notice changes its own text, with the observer watching.
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    widget.flush();
    await vi.advanceTimersByTimeAsync(1_000);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    widget.flush();
    await vi.advanceTimersByTimeAsync(9_000);
    // And a full-page pass with the notice on the page, which extracts from the body.
    const pass = widget.translate();
    await vi.advanceTimersByTimeAsync(100);
    await pass;

    const texts = everyTextSent(sent);
    for (const own of [NOTICE_EN, REPORT_EN, CLOSE_EN, "English", "Tap the text"]) {
      expect(texts, `the widget sent its own text for translation: ${own}`).not.toContain(own);
    }
    // The page and its retry. The second pass finds nothing left to send: every
    // block is already translated, and the notice is not page content.
    expect(translateRequests(sent).length).toBe(2);
    expect(document.querySelector("[data-dz-notice-hide]")?.textContent).toBe(CLOSE_EN);
    expect(document.querySelector("[data-dz-notice] [lang='en']")?.textContent).toBe(NOTICE_EN);
    expect(toggle()?.textContent).toBe("English");
    expect(document.querySelectorAll("[data-dz-notice] [lang^='dz-x']").length).toBe(0);
    // Host pages find and style the toggle by this attribute: it stays unique.
    expect(document.querySelectorAll("[data-dz-control]").length).toBe(1);
  });

  it("fr522_the_report_form_is_not_translated_either", async () => {
    const sent = server(machine);
    const widget = await start("<p id='block'>Apply for a passport.</p>");
    await switchToDzongkha();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();
    expect(document.querySelector("[data-dz-report-form]")).not.toBe(null);
    widget.flush();
    await new Promise((done) => setTimeout(done, 20));

    expect(everyTextSent(sent)).not.toContain("The meaning is wrong");
    expect(translateRequests(sent).length).toBe(1);
  });
});

describe("a report reaches the server (FR-430, FR-432)", () => {
  it("fr430_a_click_inside_a_translated_block_reports_that_block", async () => {
    const sent = server(machine);
    const widget = await start('<p>Read the <a id="link" href="#x">notice</a> first.</p>');
    const link = document.getElementById("link")!;
    expect(widget.segmentKeyFor(link)).toBe(undefined); // nothing translated yet

    await switchToDzongkha();
    expect(widget.segmentKeyFor(link)).toBe(KEY); // the link's paragraph is the block

    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    link.click();
    (document.querySelector("[data-dz-reason]") as HTMLSelectElement).value = "wrong_term";
    (document.querySelector("[data-dz-send]") as HTMLElement).click();
    await vi.waitFor(() =>
      expect(sent.some((s) => s.url.includes("/v1/feedback"))).toBe(true),
    );
    const report = sent.find((s) => s.url.includes("/v1/feedback"))!;
    expect(report.body).toEqual({ site: "portal", segment_key: KEY, reason: "wrong_term" });
  });

  it("fr430_a_block_is_not_reportable_once_the_page_is_back_in_english", async () => {
    server(machine);
    const widget = await start("<p id='block'>Apply for a passport.</p>");
    await switchToDzongkha();
    expect(widget.segmentKeyFor(document.getElementById("block")!)).toBe(KEY);
    widget.toggleBack();
    expect(widget.segmentKeyFor(document.getElementById("block")!)).toBe(undefined);
  });

  it("fr430_a_translation_without_a_key_offers_no_report", async () => {
    server((s) => ({ id: s.id, text: `DZ:${s.text}`, status: "translated", origin: "mt" }));
    const widget = await start("<p id='block'>Apply for a passport.</p>");
    await switchToDzongkha();
    expect(widget.segmentKeyFor(document.getElementById("block")!)).toBe(undefined);
  });
});

describe("sendFeedback (FR-430, FR-432)", () => {
  const report = { segmentKey: KEY, reason: "wrong_term", comment: "", website: "" };

  function api(fetchImpl: unknown, timeoutMs?: number): ApiOptions {
    return {
      base: "https://api.example",
      site: "portal",
      fetchImpl: fetchImpl as typeof fetch,
      ...(timeoutMs === undefined ? {} : { timeoutMs }),
    };
  }

  it("fr430_posts_what_the_server_validates_and_no_more", async () => {
    const calls: { url: string; body: unknown }[] = [];
    const ok = async (url: string, init?: RequestInit) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) });
      return { ok: true, status: 202 } as Response; // a 202 has no body to parse
    };
    expect(await sendFeedback(api(ok), report)).toBe(true);
    expect(await sendFeedback(api(ok), { ...report, comment: "the fee word" })).toBe(true);
    expect(calls).toEqual([
      {
        url: "https://api.example/v1/feedback",
        body: { site: "portal", segment_key: KEY, reason: "wrong_term" },
      },
      {
        url: "https://api.example/v1/feedback",
        body: { site: "portal", segment_key: KEY, reason: "wrong_term", comment: "the fee word" },
      },
    ]);
  });

  it("fr432_sends_the_honeypot_when_something_filled_it", async () => {
    let body: Record<string, unknown> = {};
    const ok = async (_: string, init?: RequestInit) => {
      body = JSON.parse(String(init?.body));
      return { ok: true, status: 202 } as Response;
    };
    await sendFeedback(api(ok), { ...report, website: "https://spam.example" });
    expect(body["website"]).toBe("https://spam.example");
  });

  it("fr430_a_refusal_or_a_dead_network_is_false_never_an_exception", async () => {
    const refused = async () => ({ ok: false, status: 403 }) as Response;
    const dead = async () => {
      throw new TypeError("network error");
    };
    expect(await sendFeedback(api(refused), report)).toBe(false);
    expect(await sendFeedback(api(dead), report)).toBe(false);
    expect(await sendFeedback(api("not a function"), report)).toBe(false);
  });

  it("fr430_a_request_that_stalls_gives_up", async () => {
    // Without a deadline the promise never settles, the form stays open and
    // the reader is never thanked.
    const stalled = (_: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      });
    expect(await sendFeedback(api(stalled, 30), report)).toBe(false);
  });
});
