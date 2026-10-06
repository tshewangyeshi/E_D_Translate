// Dzongkha typography for the blocks the widget writes (S6.1, FR-340, FR-341).
//
// Scope is everything here. Many Bhutanese sites mark their own pages as
// Dzongkha (the G2C portal's <html> is lang="dzo"), so styling [lang^="dz"]
// would restyle a host's whole page. These rules reach only elements carrying
// WRITTEN, which the widget sets on a block when it writes Dzongkha into it
// and removes when it toggles back.
//
// - Font: a self-hosted, subsetted WOFF2 (Noto Serif Tibetan, OFL) declared
//   for the Tibetan range only, so Latin text in a translated block keeps an
//   ordinary system font and the file is fetched only when Dzongkha is shown.
//   It is marked !important: host pages pin fonts with no
//   Tibetan in them (the portal's documents pin Times New Roman on every
//   run), and Dzongkha in such a font falls back to whatever the system has,
//   unreadably small on Windows.
// - Type scale: line height and size as custom properties a host can set
//   (--dzweb-dz-line-height, --dzweb-dz-scale). Applied firmly too: a host's
//   own line height is tuned for Latin, and a button with `font: inherit`
//   clipped stacked syllables in every engine tested (S6.3). The variables are
//   how a host adjusts them.
// - Size grows from the block's own original size (--dz-base), and only on
//   the outermost written block, so nested written blocks do not compound.

/** Set on every block the widget has written Dzongkha into. */
export const WRITTEN = "data-dz-written";

/**
 * Set on a block the widget left in English inside one it wrote (a Tier 1
 * notice inside a translated section): the Dzongkha rules skip its subtree,
 * and it keeps its own size (S6.1).
 */
export const ENGLISH = "data-dz-english";

/** The family name the widget's font is declared under. */
const FAMILY = "dzweb Dzongkha";

/** The Tibetan block, the zero-width characters, the dotted circle. */
const RANGE = "U+0F00-0FFF, U+200B-200D, U+25CC";

export function typographyCss(fontUrl: string): string {
  const written = `[${WRITTEN}]`;
  return [
    `@font-face{font-family:"${FAMILY}";src:url("${fontUrl}") format("woff2");` +
      `font-display:swap;unicode-range:${RANGE}}`,
    `:where(:root){--dzweb-dz-line-height:2;--dzweb-dz-scale:1.3;` +
      `--dzweb-dz-font:"${FAMILY}",system-ui,"Noto Serif Tibetan",Jomolhari,"Microsoft Himalaya",sans-serif}`,
    `${written}:not(${written} ${written}){font-size:calc(var(--dz-base,1em) * var(--dzweb-dz-scale))!important}`,
    `${written}:not([${ENGLISH}] *),${written} *:not([${ENGLISH}],[${ENGLISH}] *){` +
      `font-family:var(--dzweb-dz-font)!important;line-height:var(--dzweb-dz-line-height)!important}`,
    // English inside Dzongkha: its own size back, and an ordinary line height.
    `${written} [${ENGLISH}]{font-size:var(--dz-base,1em)!important;` +
      `font-family:var(--dz-family)!important;line-height:normal!important}`,
  ].join("\n");
}

/** Add the style block once per document. Safe to call on every write. */
export function installTypography(doc: Document, fontUrl: string): void {
  if (doc.querySelector("style[data-dz-style]") !== null) return;
  const style = doc.createElement("style");
  style.setAttribute("data-dz-style", "");
  style.textContent = typographyCss(fontUrl);
  (doc.head ?? doc.documentElement).appendChild(style);
}

/**
 * The block's font size before Dzongkha, for --dz-base. Read in one phase
 * before a batch writes anything, so the page is restyled once, not once per
 * block. A block already written keeps the size it was first measured at.
 */
export function originalSize(block: Element): string | null {
  const kept = (block as HTMLElement).style?.getPropertyValue("--dz-base");
  if (kept) return kept;
  const view = block.ownerDocument.defaultView;
  const size = view?.getComputedStyle(block).fontSize ?? "";
  return /^\d+(\.\d+)?px$/.test(size) ? size : null;
}

export function markWritten(block: Element, base: string | null): void {
  block.setAttribute(WRITTEN, "");
  if (base !== null) (block as HTMLElement).style?.setProperty("--dz-base", base);
}

/** Mark a block left English inside a written one; see ENGLISH. */
export function markEnglish(block: Element, base: string | null, family: string | null): void {
  block.setAttribute(ENGLISH, "");
  const style = (block as HTMLElement).style;
  if (base !== null) style?.setProperty("--dz-base", base);
  // Font family inherits: without its own, it would take the Dzongkha one.
  if (family !== null) style?.setProperty("--dz-family", family);
}

/** The block's font family before Dzongkha, read with its size (see originalSize). */
export function originalFamily(block: Element): string | null {
  const family = block.ownerDocument.defaultView?.getComputedStyle(block).fontFamily ?? "";
  return family === "" ? null : family;
}

/** Undo markWritten, leaving no empty style attribute behind (FR-212). */
export function clearWritten(block: Element): void {
  block.removeAttribute(WRITTEN);
  block.removeAttribute(ENGLISH);
  // Most blocks were never sized (no typography, or no layout to measure):
  // leave their style object alone, which is the expensive part to touch.
  if (!/--dz-(base|family)/.test(block.getAttribute("style") ?? "")) return;
  const style = (block as HTMLElement).style;
  if (style === undefined) return;
  style.removeProperty("--dz-base");
  style.removeProperty("--dz-family");
  if (block.getAttribute("style") === "") block.removeAttribute("style");
}
