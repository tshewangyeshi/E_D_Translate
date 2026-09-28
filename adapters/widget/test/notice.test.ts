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
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Notice, type NoticeOptions } from "../src/notice";
import { NOTICE_DZ, NOTICE_EN } from "../src/locale-dz";

const KEY = "a".repeat(64);

function make(overrides: Partial<NoticeOptions> = {}) {
  const sent: { key: string; reason: string; comment: string }[] = [];
  const notice = new Notice({
    send: async (key, reason, comment) => {
      sent.push({ key, reason, comment });
      return true;
    },
    keyFor: () => KEY,
    ...overrides,
  });
  return { notice, sent };
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
    expect(root.getAttribute("role")).toBe("status");
    expect(root.getAttribute("aria-live")).toBe("polite"); // never interrupt mid-sentence
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

    expect(sent[0]).toEqual({ key: KEY, reason: "wrong_term", comment: "the fee word" });
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
    expect(values).toEqual([
      "wrong_meaning",
      "wrong_term",
      "not_translated",
      "formatting",
      "offensive",
      "other",
    ]);
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
    expect(comment.maxLength).toBe(500);
  });
});
