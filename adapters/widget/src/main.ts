// Boot the widget from a single script tag (FR-200).
//
//   <script type="module" src="https://…/main.js" data-dz-site="portal"></script>
//
// No host build step, no framework, no polyfills. This file only reads its own
// configuration, offers a control, and delegates. It is wrapped end to end
// because a translation widget must never be the reason a government page
// fails to load (FR-215).

import { LABEL_SWITCH_TO_DZ, LABEL_SWITCH_TO_EN } from "./locale-dz.js";
import { sendFeedback } from "./api.js";
import { Notice } from "./notice.js";
import { Widget, savePreference, savedPreference, whenQuiet } from "./widget.js";

/** The script element that loaded this module, for its data- attributes. */
function ownScript(): HTMLScriptElement | null {
  const byUrl = document.querySelector<HTMLScriptElement>(`script[src="${import.meta.url}"]`);
  return byUrl ?? document.querySelector<HTMLScriptElement>("script[data-dz-site]");
}

function control(label: string, onClick: () => void): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("data-dz-control", "");
  // The host page owns its own styling; this stays visually neutral and is
  // positioned by the site's CSS via the data attribute.
  button.addEventListener("click", onClick);
  return button;
}

/**
 * A page the site has marked as carrying personal data (NFR-304).
 *
 * Checked on the document and the body, because a template that wraps every
 * signed-in page usually owns one or the other. This is stronger than skipping
 * regions: the widget does not load, does not offer a control, and never reads
 * the page at all. A citizen's own application form should not be sampled by a
 * translation service even to decide there is nothing to translate.
 */
function pageIsPrivate(): boolean {
  return (
    document.documentElement.hasAttribute("data-dz-private") ||
    document.body?.hasAttribute("data-dz-private") === true
  );
}

export async function boot(): Promise<Widget | null> {
  if (pageIsPrivate()) return null;
  const script = ownScript();
  const site = script?.getAttribute("data-dz-site");
  if (!site) return null; // nothing to do without a site id

  const base = script?.getAttribute("data-dz-api") ?? new URL(".", import.meta.url).origin;
  const api = { base, site };
  const widget = new Widget(api, () => document.body);
  const notice = new Notice({
    send: (key, reason, comment) => sendFeedback(api, key, reason, comment),
    keyFor: (block) => widget.segmentKeyFor(block),
  });

  // Configuration first: without it the widget cannot tell Tier 1 or private
  // regions apart, so it offers no control at all (FR-216).
  if (!(await widget.start())) return null;

  const button = control(LABEL_SWITCH_TO_DZ, () => {
    void (async () => {
      if (widget.language === "en") {
        savePreference("dz");
        await widget.translate();
        // The notice appears only once machine output is actually on the
        // page: an approved translation needs no warning (FR-520).
        if (widget.showingMachineOutput) notice.show();
        button.textContent = LABEL_SWITCH_TO_EN;
      } else {
        savePreference("en");
        widget.toggleBack();
        notice.hide();
        button.textContent = LABEL_SWITCH_TO_DZ;
      }
    })();
  });
  document.body.appendChild(button);

  // Watch from the start, not from the first translation: content added while
  // the page is still in English must be known about, so it can be translated
  // the moment the reader switches (FR-211).
  widget.watch();

  if (savedPreference() === "dz") {
    // Wait for the host to finish rendering and settle before touching it, so
    // the first write cannot race a framework's hydration pass (ER-16).
    whenQuiet(() => {
      void widget.translate().then(() => {
        if (widget.showingMachineOutput) notice.show();
        button.textContent = LABEL_SWITCH_TO_EN;
      });
    });
  }
  return widget;
}

// Self-start, but never let a failure escape into the host page.
void (async () => {
  try {
    await boot();
  } catch {
    /* the page stays in English */
  }
})();
