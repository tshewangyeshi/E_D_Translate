// The only home for Dzongkha-specific logic in the widget (NFR-500).
// Its Python counterpart is orchestrator/locale/dz.py; the shared break rules
// are kept equal by tests/fixtures, not by memory.
//
// Everything here is a RENDER concern. Zero-width spaces exist so a browser can
// wrap a Tibetan line; they are inserted on the way to the screen and never
// travel back into a cache, translation memory or TTS input (FR-160). The
// widget therefore keeps the server's text as the value it compares against,
// and renders a separate string.

/** Tibetan syllable separator; the only legal break opportunity in Dzongkha. */
export const TSHEG = "\u0F0B";

/** Zero-width space: a break opportunity that renders as nothing. */
export const ZWSP = "\u200B";

/** Characters inserted purely for rendering, stripped before any comparison. */
const RENDER_ARTEFACTS = [ZWSP, "\u200C", "\uFEFF"];

/**
 * Insert break opportunities after each tsheg (FR-160).
 *
 * Dzongkha has no spaces, so without this a long line cannot wrap and overflows
 * its container. The tsheg is where a break is allowed, so a zero-width space
 * goes after each one.
 */
export function insertBreaks(text: string): string {
  return text.split(TSHEG).join(TSHEG + ZWSP);
}

/** Remove everything `insertBreaks` added, recovering the stored text. */
export function stripRenderArtefacts(text: string): string {
  let out = text;
  for (const artefact of RENDER_ARTEFACTS) out = out.split(artefact).join("");
  return out;
}

/** True when the text carries Tibetan script and therefore needs break handling. */
export function isTibetan(text: string): boolean {
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0;
    if (code >= 0x0f00 && code <= 0x0fff) return true;
  }
  return false;
}

/**
 * Control labels.
 *
 * These live here, not in the widget, because NFR-500 keeps every piece of
 * Dzongkha in one file: someone reviewing the language should not have to read
 * the widget to find it. Written as escapes so the text cannot be mangled by an
 * editor or a tool that does not handle the script.
 */
export const LABEL_SWITCH_TO_DZ = "\u0F62\u0FAB\u0F7C\u0F44\u0F0B\u0F41";  // Dzongkha
export const LABEL_SWITCH_TO_EN = "English";

/**
 * Machine-translation notice (FR-520, FR-522).
 *
 * Bilingual on purpose. A reader who cannot read Dzongkha well enough to judge
 * the translation is exactly the reader who most needs to be told it is
 * machine output, and a reader who reads only Dzongkha needs the same warning.
 * Showing one language would leave one of them uninformed.
 *
 * Written as escapes so the text survives any editor or tool that mangles the
 * script, and so this file stays the only place Dzongkha lives (NFR-500).
 */
export const NOTICE_DZ =
  // "This page was translated by machine. It may contain mistakes."
  "\u0F62\u0FAB\u0F7C\u0F44\u0F0B\u0F41\u0F0B\u0F60\u0F51\u0F72\u0F0B \u0F60\u0F55\u0FB2\u0F74\u0F63\u0F0B\u0F62\u0F72\u0F42\u0F0B\u0F42\u0F72\u0F66\u0F0B\u0F66\u0F92\u0FB1\u0F74\u0F62\u0F0B\u0F61\u0F7C\u0F51\u0F54\u0F0B\u0F68\u0F72\u0F53\u0F0D \u0F53\u0F7C\u0F62\u0F0B\u0F60\u0F41\u0FB2\u0F74\u0F63\u0F0B\u0F61\u0F7C\u0F51\u0F0B\u0F66\u0FB2\u0F72\u0F51\u0F0D";

export const NOTICE_EN =
  "Translated by machine. It may contain mistakes.";

/** The report-an-error control (FR-522), bilingual for the same reason. */
export const REPORT_DZ = "\u0F53\u0F7C\u0F62\u0F0B\u0F60\u0F41\u0FB2\u0F74\u0F63\u0F0B\u0F66\u0F99\u0F53\u0F0B\u0F5E\u0F74\u0F0D";  // report an error
export const REPORT_EN = "Report an error";

/** Shown while the reader is choosing which block is wrong. */
export const PICK_DZ = "\u0F53\u0F7C\u0F62\u0F0B\u0F56\u0F60\u0F72\u0F0B\u0F61\u0F72\u0F42\u0F0B\u0F5A\u0F72\u0F42\u0F0B\u0F60\u0F51\u0F72\u0F0B\u0F63\u0F74\u0F0B\u0F68\u0F7A\u0F56\u0F0D";
export const PICK_EN = "Tap the text that is wrong";

export const THANKS_DZ = "\u0F56\u0F40\u0F60\u0F0B\u0F51\u0FB2\u0F72\u0F53\u0F0B\u0F46\u0F7A\u0F0D";  // thank you
export const THANKS_EN = "Thank you";

export const CLOSE_EN = "Hide";

/**
 * Shown beside the comment box (NFR-303). English only for now: the Dzongkha
 * wording has to come from DCDD, not from a developer's guess (TODOS.md).
 */
export const HINT_EN = "Do not include names, ID numbers or contact details.";
