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

import {
  CLOSE_EN,
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

export interface NoticeOptions {
  /** Sends a report; resolves whether the request was made, not whether it was kept. */
  send: (segmentKey: string, reason: string, comment: string) => Promise<boolean>;
  /** The reportable key for a block, or undefined when the widget did not translate it. */
  keyFor: (block: Element) => string | undefined;
  document?: Document;
}

function el(doc: Document, tag: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (text !== undefined) node.textContent = text; // text, never markup
  return node;
}

export class Notice {
  private root: HTMLElement | null = null;
  private picking = false;
  private readonly doc: Document;

  constructor(private readonly options: NoticeOptions) {
    this.doc = options.document ?? document;
  }

  get visible(): boolean {
    return this.root !== null && this.root.isConnected;
  }

  get selecting(): boolean {
    return this.picking;
  }

  /** Show the notice. Safe to call repeatedly; only one is ever present. */
  show(): void {
    if (this.visible) return;
    const root = el(this.doc, "aside");
    root.setAttribute("data-dz-notice", "");
    // Announced, but politely: it must not interrupt a screen reader mid-sentence.
    root.setAttribute("role", "status");
    root.setAttribute("aria-live", "polite");

    const dz = el(this.doc, "span", NOTICE_DZ);
    dz.setAttribute("lang", "dz");
    const en = el(this.doc, "span", NOTICE_EN);
    en.setAttribute("lang", "en");
    root.append(dz, en);

    const report = el(this.doc, "button", `${REPORT_DZ} / ${REPORT_EN}`) as HTMLButtonElement;
    report.type = "button";
    report.setAttribute("data-dz-report", "");
    report.addEventListener("click", () => this.startPicking());

    const hide = el(this.doc, "button", CLOSE_EN) as HTMLButtonElement;
    hide.type = "button";
    hide.setAttribute("data-dz-notice-hide", "");
    // This page view only. Nothing is stored, deliberately (FR-521).
    hide.addEventListener("click", () => root.setAttribute("hidden", ""));

    root.append(report, hide);
    this.doc.body.appendChild(root);
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
    this.doc.addEventListener("click", this.onPick, true);
  }

  private stopPicking(): void {
    if (!this.picking) return;
    this.picking = false;
    this.doc.removeEventListener("click", this.onPick, true);
  }

  /** Capture-phase, so choosing a block never triggers the host page's own handler. */
  private readonly onPick = (event: Event): void => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (this.root !== null && this.root.contains(target)) return; // our own controls
    event.preventDefault();
    event.stopPropagation();
    this.stopPicking();

    const key = this.options.keyFor(target);
    if (key === undefined) {
      this.setStatus(`${REPORT_DZ} / ${REPORT_EN}`);
      return; // not a block we translated: nothing to report against
    }
    this.openForm(key);
  };

  private openForm(segmentKey: string): void {
    const root = this.root;
    if (root === null) return;
    const form = el(this.doc, "div");
    form.setAttribute("data-dz-report-form", "");

    const select = this.doc.createElement("select");
    select.setAttribute("data-dz-reason", "");
    for (const [value, label] of REASONS) {
      const option = this.doc.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    const comment = this.doc.createElement("textarea");
    comment.setAttribute("data-dz-comment", "");
    comment.maxLength = 500; // matches the server; refused rather than truncated there
    comment.placeholder = "Optional";

    const send = el(this.doc, "button", "Send") as HTMLButtonElement;
    send.type = "button";
    send.setAttribute("data-dz-send", "");
    send.addEventListener("click", () => {
      void this.options.send(segmentKey, select.value, comment.value).then(() => {
        // Always the same acknowledgement. The server answers 202 whether it
        // kept the report or dropped it, and telling the reader otherwise
        // would be inventing a distinction the server refuses to make.
        form.remove();
        this.setStatus(`${THANKS_DZ} / ${THANKS_EN}`);
      });
    });

    form.append(select, comment, send);
    root.appendChild(form);
  }

  private setStatus(text: string): void {
    const button = this.root?.querySelector("[data-dz-report]");
    if (button !== null && button !== undefined) button.textContent = text;
  }
}
