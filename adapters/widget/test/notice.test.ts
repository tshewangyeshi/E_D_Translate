// @vitest-environment jsdom
// S3.2 — the machine-translation notice.
// Requirements: FR-520, FR-521, FR-522, FR-430.
//
// A citizen has to be able to tell whether they are reading something a person
// approved or something a machine produced. Get that wrong and someone acts on
// a mistranslated fee or deadline believing the government said it. These tests
// hold the notice to that: it appears whenever machine output is on the page,
// it cannot be dismissed in a way that carries to the next page, and the report
// control actually reaches the server.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { Notice, type NoticeOptions, type Report } from "../src/notice";
import { HINT_EN, NOTICE_DZ, NOTICE_EN } from "../src/locale-dz";

const KEY = "a".repeat(64);

// The same file the server's tests read, so the two sides cannot drift.
const CONTRACT = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../tests/fixtures/feedback/contract.json"), "utf8"),
) as { reasons: string[]; max_comment_chars: number; honeypot_field: string };

function make(overrides: Partial<NoticeOptions> = {}) {
  const sent: Report[] = [];
  const notice = new Notice({
    send: async (report) => {
      sent.push(report);
      return true;
    },
    keyFor: () => KEY,
    ...overrides,
  });
  return { notice, sent };
}

/** Show the notice, start a report and pick the block. */
function openForm(overrides: Partial<NoticeOptions> = {}) {
  const made = make(overrides);
  made.notice.show();
  (document.querySelector("[data-dz-report]") as HTMLElement).click();
  document.getElementById("block")!.click();
  return made;
}

beforeEach(() => {
  document.body.innerHTML = "<p id='block'>DZ:Some translated text.</p>";
});

describe("the notice itself (FR-520, FR-521)", () => {
  it("fr520_is_bilingual", () => {
    // A reader who cannot judge the Dzongkha most needs the warning, and a
    // reader who reads only Dzongkha needs it too. One language leaves one of
    // them uninformed.
    const { notice } = make();
    notice.show();
    const text = document.querySelector("[data-dz-notice]")?.textContent ?? "";
    expect(text).toContain(NOTICE_EN);
    expect(text).toContain(NOTICE_DZ);
  });

  it("fr520_marks_each_language_for_assistive_technology", () => {
    const { notice } = make();
    notice.show();
    const root = document.querySelector("[data-dz-notice]") as HTMLElement;
    expect(root.querySelector("[lang='dz']")).not.toBe(null);
    expect(root.querySelector("[lang='en']")).not.toBe(null);
    const live = root.querySelector("[role='status']") as HTMLElement;
    expect(live.getAttribute("aria-live")).toBe("polite"); // never interrupt mid-sentence
    expect(live.textContent).toBe(NOTICE_DZ + NOTICE_EN); // the message, not the buttons
    expect(root.hasAttribute("role")).toBe(false); // an <aside> may not be a status (S4.4)
  });

  it("fr520_only_one_notice_exists_however_often_it_is_shown", () => {
    const { notice } = make();
    notice.show();
    notice.show();
    notice.show();
    expect(document.querySelectorAll("[data-dz-notice]").length).toBe(1);
  });

  it("fr521_hiding_stores_nothing", () => {
    // The whole of FR-521 in one assertion: a reader who dismissed the notice
    // on another page has not thereby been told about this one.
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-notice-hide]") as HTMLElement).click();
    expect(document.querySelector("[data-dz-notice]")?.hasAttribute("hidden")).toBe(true);

    const stored = Object.keys(localStorage).concat(Object.keys(sessionStorage));
    expect(stored.filter((k) => k.includes("notice"))).toEqual([]);

    // A fresh page load shows it again.
    notice.hide();
    document.body.innerHTML = "<p>DZ:Another page.</p>";
    const second = make().notice;
    second.show();
    const root = document.querySelector("[data-dz-notice]");
    expect(root?.hasAttribute("hidden")).toBe(false);
  });

  it("fr520_disappears_when_the_reader_returns_to_english", () => {
    const { notice } = make();
    notice.show();
    notice.hide();
    expect(document.querySelector("[data-dz-notice]")).toBe(null);
  });
});

describe("reporting an error (FR-522, FR-430)", () => {
  it("fr522_reporting_reaches_the_server_with_the_blocks_key", async () => {
    const { notice, sent } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    expect(notice.selecting).toBe(true);

    document.getElementById("block")!.click();
    (document.querySelector("[data-dz-reason]") as HTMLSelectElement).value = "wrong_term";
    (document.querySelector("[data-dz-comment]") as HTMLTextAreaElement).value = "the fee word";
    (document.querySelector("[data-dz-send]") as HTMLElement).click();
    await vi.waitFor(() => expect(sent.length).toBe(1));

    expect(sent[0]).toEqual({
      segmentKey: KEY,
      reason: "wrong_term",
      comment: "the fee word",
      website: "",
    });
  });

  it("fr522_one_tap_sends_one_report_however_slow_the_network", async () => {
    // Each extra POST spends the reader's allowance of ten an hour.
    let finish: (ok: boolean) => void = () => {};
    const sent: Report[] = [];
    openForm({
      send: (report) => {
        sent.push(report);
        return new Promise<boolean>((done) => (finish = done));
      },
    });
    const send = document.querySelector("[data-dz-send]") as HTMLButtonElement;
    send.click();
    send.click();
    send.click();
    expect(send.disabled).toBe(true);
    expect(sent.length).toBe(1);
    finish(true);
    await vi.waitFor(() => expect(document.querySelector("[data-dz-report-form]")).toBe(null));
  });

  it("fr522_picking_a_second_block_replaces_the_form", () => {
    const { notice } = openForm();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();
    expect(document.querySelectorAll("[data-dz-report-form]").length).toBe(1);
    expect(notice.selecting).toBe(false);
  });

  it("fr522_escape_leaves_picking_and_gives_the_page_back", () => {
    // While picking, every click on the page is swallowed. A reader who
    // changed their mind must not be left with a page that ignores them.
    const clicks: string[] = [];
    document.getElementById("block")!.addEventListener("click", () => clicks.push("host"));
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    expect(notice.selecting).toBe(true);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(notice.selecting).toBe(false);
    expect(document.querySelector("[data-dz-report]")?.textContent).toContain("Report an error");
    document.getElementById("block")!.click();
    expect(clicks).toEqual(["host"]);
  });

  it("fr522_the_readers_own_toggle_still_works_while_picking", () => {
    // Regression: the capture-phase handler swallowed a tap on the widget's
    // language button, so the reader had to tap it twice.
    document.body.innerHTML +=
      "<button id='toggle' data-dz-control>English</button>";
    const taps: string[] = [];
    document.getElementById("toggle")!.addEventListener("click", () => taps.push("toggle"));
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("toggle")!.click();
    expect(taps).toEqual(["toggle"]);
    expect(notice.selecting).toBe(true); // still picking: that tap was not a pick
  });

  it("fr522_a_keyboard_reader_picks_the_block_holding_their_selection", async () => {
    const { notice, sent } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();

    const block = document.getElementById("block")!;
    const range = document.createRange();
    range.selectNodeContents(block);
    const selection = document.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));

    expect(notice.selecting).toBe(false);
    expect(document.querySelector("[data-dz-report-form]")).not.toBe(null);
    (document.querySelector("[data-dz-send]") as HTMLElement).click();
    await vi.waitFor(() => expect(sent.length).toBe(1));
    selection.removeAllRanges();
  });

  it("fr522_a_keyboard_reader_picks_the_focused_element", () => {
    document.body.innerHTML = "<a id='link' href='#x'>DZ:A link</a>";
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getSelection()?.removeAllRanges();
    (document.getElementById("link") as HTMLElement).focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(document.querySelector("[data-dz-report-form]")).not.toBe(null);
  });

  it("fr522_hiding_the_notice_also_leaves_picking", () => {
    const clicks: string[] = [];
    document.getElementById("block")!.addEventListener("click", () => clicks.push("host"));
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    (document.querySelector("[data-dz-notice-hide]") as HTMLElement).click();
    expect(notice.selecting).toBe(false);
    document.getElementById("block")!.click();
    expect(clicks).toEqual(["host"]);
  });

  it("fr432_the_form_carries_a_honeypot_a_reader_cannot_reach", async () => {
    // The server drops any report whose `website` is filled. That only works
    // if there is a field for a form-filling bot to fill.
    const { sent } = openForm();
    const trap = document.querySelector("[data-dz-website]") as HTMLInputElement;
    expect(trap.name).toBe(CONTRACT.honeypot_field);
    expect(trap.tabIndex).toBe(-1); // not reachable by keyboard
    expect(trap.getAttribute("aria-hidden")).toBe("true"); // nor by screen reader
    expect(trap.hasAttribute("hidden")).toBe(false); // bots skip fields that are not rendered
    expect(trap.style.left).toBe("-9999px");

    trap.value = "https://spam.example";
    (document.querySelector("[data-dz-send]") as HTMLElement).click();
    await vi.waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0]!.website).toBe("https://spam.example");
  });

  it("nfr303_the_reader_is_told_not_to_include_personal_details", () => {
    openForm();
    expect(document.querySelector("[data-dz-hint]")?.textContent).toBe(HINT_EN);
    expect(HINT_EN).toMatch(/names/i);
  });

  it("fr522_choosing_a_block_does_not_trigger_the_hosts_own_handler", () => {
    // Picking runs in the capture phase: a reader reporting an error on a link
    // must not also follow it.
    const clicks: string[] = [];
    document.body.innerHTML = "<a id='link' href='#x'>DZ:A link</a>";
    document.getElementById("link")!.addEventListener("click", () => clicks.push("host"));

    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("link")!.click();

    expect(clicks).toEqual([]);
    expect(notice.selecting).toBe(false);
  });

  it("fr522_a_block_the_widget_did_not_translate_offers_no_report", () => {
    const { notice, sent } = make({ keyFor: () => undefined });
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();

    expect(document.querySelector("[data-dz-report-form]")).toBe(null);
    expect(sent).toEqual([]);
    expect(notice.selecting).toBe(false);
  });

  it("fr430_the_reason_list_matches_the_servers_closed_list", () => {
    // The server refuses anything outside its list, silently recording "other".
    // If these drift apart, readers' reports quietly lose their reason.
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();

    const values = [...document.querySelectorAll("[data-dz-reason] option")].map(
      (o) => (o as HTMLOptionElement).value,
    );
    expect(values).toEqual(CONTRACT.reasons);
  });

  it("fr522_the_reader_is_thanked_the_same_way_whatever_the_server_did", async () => {
    // The server answers 202 whether it kept the report or dropped it. Showing
    // the reader a different outcome would invent a distinction the server
    // deliberately refuses to make.
    const { notice } = make({ send: async () => false });
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();
    (document.querySelector("[data-dz-send]") as HTMLElement).click();

    await vi.waitFor(() =>
      expect(document.querySelector("[data-dz-report-form]")).toBe(null),
    );
    expect(document.querySelector("[data-dz-report]")?.textContent).toContain("Thank you");
  });

  it("fr430_the_comment_is_capped_to_what_the_server_accepts", () => {
    const { notice } = make();
    notice.show();
    (document.querySelector("[data-dz-report]") as HTMLElement).click();
    document.getElementById("block")!.click();
    const comment = document.querySelector("[data-dz-comment]") as HTMLTextAreaElement;
    expect(comment.maxLength).toBe(CONTRACT.max_comment_chars);
  });
});
