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
export const LABEL_SWITCH_TO_DZ = "\u0F62\u0FB1\u0F7C\u0F44\u0F0B\u0F41";  // Dzongkha
export const LABEL_SWITCH_TO_EN = "English";
