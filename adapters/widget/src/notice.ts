// The machine-translation notice (S3.2).
// Requirements: FR-520, FR-521, FR-522, FR-430.
//
// A citizen reading a government page in Dzongkha has to be able to tell
// whether they are reading something a person approved or something a machine
// produced. Get that wrong and the service does real harm: someone acts on a
// mistranslated fee, deadline or eligibility rule believing the government
// said it.
//
// So the notice is not a nicety and not dismissable in any way that carries to
// the next page. `Hide` collapses it for this page view only; there is no
// stored preference, because a reader who dismissed it on the homepage last
// month is not thereby informed about the page they are reading today
// (FR-521).
//
// It is built from DOM nodes rather than innerHTML for the same reason the
// rest of the widget is: nothing here is ever parsed as markup (NFR-300).
//
// The notice lives inside the page the widget translates, so it has to say
// that it is not page content. `translate="no"` keeps it out of extraction and
// the observer skips `data-dz-notice` as it skips the toggle's
// `data-dz-control`. Without both, the widget sent its own warning to the
// model and wrote the answer over its buttons. (It does not reuse
// `data-dz-control`: host pages select and style the toggle by that.)

import { OWN_UI } from "./widget.js";
import {
  CLOSE_EN,
  HINT_EN,
  NOTICE_DZ,
  NOTICE_EN,
  PICK_DZ,
  PICK_EN,
  REPORT_DZ,
  REPORT_EN,
  THANKS_DZ,
  THANKS_EN,
} from "./locale-dz.js";

/** Reasons a reader can give, paired with the server's closed list (FR-430). */
const REASONS: ReadonlyArray<readonly [string, string]> = [
  ["wrong_meaning", "The meaning is wrong"],
  ["wrong_term", "A term is wrong"],
  ["not_translated", "Not translated"],
  ["formatting", "Layout or numbers are wrong"],
  ["offensive", "Offensive or inappropriate"],
  ["other", "Something else"],
];

/** Matches the server; a longer comment is refused there rather than truncated. */
const MAX_COMMENT_CHARS = 500;

export interface Report {
  segmentKey: string;
  reason: string;
  comment: string;
  /** The honeypot. A reader never sees the field, so a value means a bot (FR-432). */
  website: string;
}

export interface NoticeOptions {
  /** Sends a report; resolves whether the request was made, not whether it was kept. */
  send: (report: Report) => Promise<boolean>;
  /** The reportable key for a block, or undefined when the widget did not translate it. */
  keyFor: (block: Element) => string | undefined;
}

/** The widget's own UI: the language toggle, and the notice with its form. */
function isOurs(element: Element): boolean {
  return element.closest(OWN_UI) !== null;
}

function el(tag: string, text?: string): HTMLElement {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text; // text, never markup
  return node;
}

export class Notice {
  private root: HTMLElement | null = null;
  private picking = false;

  constructor(private readonly options: NoticeOptions) {}

  get visible(): boolean {
    return this.root !== null && this.root.isConnected;
  }

  get selecting(): boolean {
    return this.picking;
  }

  /** Show the notice. Safe to call repeatedly; only one is ever present. */
  show(): void {
    if (this.visible) return;
    const root = el("aside");
    root.setAttribute("data-dz-notice", "");
    root.setAttribute("translate", "no"); // widget UI, not page content
    // Announced, but politely: it must not interrupt a screen reader mid-sentence.
    root.setAttribute("role", "status");
    root.setAttribute("aria-live", "polite");

    const dz = el("span", NOTICE_DZ);
    dz.setAttribute("lang", "dz");
    const en = el("span", NOTICE_EN);
    en.setAttribute("lang", "en");
    root.append(dz, en);

    const report = el("button", `${REPORT_DZ} / ${REPORT_EN}`) as HTMLButtonElement;
    report.type = "button";
    report.setAttribute("data-dz-report", "");
    report.addEventListener("click", () => this.startPicking());

    const hide = el("button", CLOSE_EN) as HTMLButtonElement;
    hide.type = "button";
    hide.setAttribute("data-dz-notice-hide", "");
    // This page view only. Nothing is stored, deliberately (FR-521).
    hide.addEventListener("click", () => {
      this.stopPicking();
      root.setAttribute("hidden", "");
    });

    root.append(report, hide);
    document.body.appendChild(root);
    this.root = root;
  }

  /** Remove the notice, for when the reader switches back to English. */
  hide(): void {
    this.stopPicking();
    this.root?.remove();
    this.root = null;
  }

  private startPicking(): void {
    if (this.picking) return;
    this.picking = true;
    this.setStatus(`${PICK_DZ} / ${PICK_EN}`);
    document.addEventListener("click", this.onPick, true);
    document.addEventListener("keydown", this.onKey, true);
  }

  private stopPicking(): void {
    if (!this.picking) return;
    this.picking = false;
    document.removeEventListener("click", this.onPick, true);
    document.removeEventListener("keydown", this.onKey, true);
    this.setStatus(`${REPORT_DZ} / ${REPORT_EN}`);
  }

  /**
   * Keyboard picking. Escape leaves, so a reader who changed their mind gets
   * the page back. Enter picks the block holding the text the reader has
   * selected, or the element that has focus: a paragraph cannot take focus,
   * but it can be selected with the keyboard.
   */
  private readonly onKey = (event: Event): void => {
    const key = (event as KeyboardEvent).key;
    if (key === "Escape") {
      this.stopPicking();
      return;
    }
    if (key !== "Enter") return;
    const anchor = document.getSelection()?.anchorNode ?? null;
    const selected = anchor instanceof Element ? anchor : (anchor?.parentElement ?? null);
    const focused = document.activeElement !== document.body ? document.activeElement : null;
    const target = selected ?? focused;
    if (target === null || isOurs(target)) return; // Enter on our own button works as usual
    event.preventDefault();
    event.stopPropagation();
    this.pick(target);
  };

  /** Capture-phase, so choosing a block never triggers the host page's own handler. */
  private readonly onPick = (event: Event): void => {
    const target = event.target;
    if (!(target instanceof Element) || isOurs(target)) return; // our own controls still work
    event.preventDefault();
    event.stopPropagation();
    this.pick(target);
  };

  private pick(target: Element): void {
    this.stopPicking();
    const key = this.options.keyFor(target);
    if (key === undefined) return; // not a block we translated: nothing to report against
    this.openForm(key);
  }

  private openForm(segmentKey: string): void {
    const root = this.root;
    if (root === null) return;
    root.querySelector("[data-dz-report-form]")?.remove(); // one form at a time
    const form = el("div");
    form.setAttribute("data-dz-report-form", "");

    const select = document.createElement("select");
    select.setAttribute("data-dz-reason", "");
    for (const [value, label] of REASONS) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    const comment = document.createElement("textarea");
    comment.setAttribute("data-dz-comment", "");
    comment.maxLength = MAX_COMMENT_CHARS;
    comment.placeholder = "Optional";

    // Said before the reader types, not after: the comment is stored (NFR-303).
    const hint = el("small", HINT_EN);
    hint.setAttribute("data-dz-hint", "");
    hint.setAttribute("lang", "en");

    // Off-screen rather than `hidden`: form-filling bots skip fields that are
    // not rendered, and fill ones that are. Set through the style object, so
    // a host Content-Security-Policy that forbids inline styles still allows it.
    const trap = document.createElement("input");
    trap.type = "text";
    trap.name = "website";
    trap.tabIndex = -1;
    trap.autocomplete = "off";
    trap.setAttribute("aria-hidden", "true");
    trap.setAttribute("data-dz-website", "");
    trap.style.position = "absolute";
    trap.style.left = "-9999px";

    const send = el("button", "Send") as HTMLButtonElement;
    send.type = "button";
    send.setAttribute("data-dz-send", "");
    send.addEventListener("click", () => {
      send.disabled = true; // one tap, one report, however slow the network is
      const report = {
        segmentKey,
        reason: select.value,
        comment: comment.value,
        website: trap.value,
      };
      void this.options.send(report).then(() => {
        // Always the same acknowledgement. The server answers 202 whether it
        // kept the report or dropped it, and telling the reader otherwise
        // would be inventing a distinction the server refuses to make.
        form.remove();
        this.setStatus(`${THANKS_DZ} / ${THANKS_EN}`);
      });
    });

    form.append(select, comment, hint, trap, send);
    root.appendChild(form);
  }

  private setStatus(text: string): void {
    const button = this.root?.querySelector("[data-dz-report]");
    if (button !== null && button !== undefined) button.textContent = text;
  }
}
