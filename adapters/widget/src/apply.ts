// Writing a translation into the host page (FR-210, FR-212, FR-214, FR-217).
//
// The rule that shapes this file: the widget may change `nodeValue` on text
// nodes that already exist and nothing else. It never creates, removes, moves
// or replaces a node. A host framework holds references to those nodes and will
// re-render from its own state; if we restructure the DOM underneath React or
// Vue, the next render either throws or silently discards the page.
//
// Every guard below fails the same way: leave the block in English. A block
// that stays English is a page that still works.

import type { Segment } from "./extract.js";
import { insertBreaks, stripRenderArtefacts } from "./locale-dz.js";
import { decodeWire, sameMarkers } from "./wire.js";

/** What the widget remembers about one block. */
export interface BlockState {
  /** The extraction this state belongs to, so restoring needs no re-extraction. */
  segment: Segment;
  /** Slot texts as they were in English, for restoring on toggle (FR-212). */
  original: string[];
  /** Slot texts the widget last wrote, to tell its own writes from the host's. */
  lastWritten: string[];
  /** Bumped on every re-extraction, so a late response can be recognised. */
  generation: number;
  /** True while Dzongkha is displayed in this block. */
  translated: boolean;
  /** Hash of the masked source, so a reader can report this block (FR-430). */
  segmentKey?: string;
}

/** Per-block state, weakly held so a removed block is collectable (spec ER-19). */
export type Registry = WeakMap<Element, BlockState>;

/** Current text of each slot, concatenating the nodes that back it. */
export function readSlots(segment: Segment): string[] {
  return segment.slots.map((nodes) => nodes.map((n) => n.nodeValue ?? "").join(""));
}

/**
 * True when the block still holds exactly what the widget last put there.
 *
 * A mismatch means the host rewrote the text while the request was in flight,
 * so the answer describes text that is no longer on the page (FR-217).
 */
export function unchangedSinceWrite(segment: Segment, expected: readonly string[]): boolean {
  const now = readSlots(segment);
  if (now.length !== expected.length) return false;
  return now.every((text, n) => stripRenderArtefacts(text) === expected[n]);
}

/**
 * Write `translated` (wire text from the server) into the segment's own nodes.
 *
 * Returns false without touching the page when the translation cannot be placed
 * exactly: malformed wire, a different marker sequence, a different slot count,
 * or text that belongs in a position with no text node to hold it.
 */
export function applyTranslation(segment: Segment, translated: string): boolean {
  const source = decodeWire(segment.text);
  const target = decodeWire(translated);
  if (source === null || target === null) return false;
  if (!sameMarkers(source, target)) return false;
  if (target.slots.length !== segment.slots.length) return false;

  // Check every slot is placeable BEFORE writing any of them: a half-written
  // block is a page in two languages with the second half misaligned.
  for (let n = 0; n < segment.slots.length; n += 1) {
    const nodes = segment.slots[n] as Text[];
    const text = target.slots[n] as string;
    if (nodes.length === 0 && text.length > 0) return false;
  }

  for (let n = 0; n < segment.slots.length; n += 1) {
    writeSlot(segment.slots[n] as Text[], target.slots[n] as string);
  }
  return true;
}

/** Restore the English a block started with, node for node. */
export function restoreOriginal(segment: Segment, original: readonly string[]): boolean {
  if (original.length !== segment.slots.length) return false;
  for (let n = 0; n < segment.slots.length; n += 1) {
    writeSlot(segment.slots[n] as Text[], original[n] as string, false);
  }
  return true;
}

/**
 * Put `text` into `nodes`, which already exist.
 *
 * A slot can be backed by several adjacent text nodes, and a translation is one
 * run of text with no way to say where one node should end and the next begin.
 * The whole run goes into the first node and the rest are emptied. They remain
 * in the document, in order, still owned by whatever put them there.
 */
function writeSlot(nodes: Text[], text: string, render = true): void {
  if (nodes.length === 0) return;
  const value = render ? insertBreaks(text) : text;
  (nodes[0] as Text).nodeValue = value;
  for (let n = 1; n < nodes.length; n += 1) (nodes[n] as Text).nodeValue = "";
}

/** What the widget remembers about one translated attribute (FR-113). */
export interface AttributeState {
  original: string;
  lastWritten: string;
}

/**
 * Write a translated attribute value.
 *
 * Attributes carry no inline markup and therefore no slots, so this is a plain
 * value swap -- but the same rule applies: only write if the value is still the
 * one we asked about, or a host update would be silently reverted.
 */
export function applyAttribute(
  element: Element,
  attr: string,
  translated: string,
  expected: string,
): boolean {
  if ((element.getAttribute(attr) ?? "") !== expected) return false;
  element.setAttribute(attr, insertBreaks(translated));
  return true;
}

/** Put an attribute back, unless the host has changed it since we wrote. */
export function restoreAttribute(element: Element, attr: string, state: AttributeState): boolean {
  const now = element.getAttribute(attr) ?? "";
  if (stripRenderArtefacts(now) !== state.lastWritten) return false; // host owns it now
  element.setAttribute(attr, state.original);
  return true;
}

/** Mark a block's language for assistive technology and styling (FR-214). */
export function markLanguage(block: Element, origin: string | undefined): void {
  block.setAttribute("lang", origin === "human" ? "dz" : "dz-x-mtfrom-en");
}

/** Put the block back to the page's own language. */
export function clearLanguage(block: Element): void {
  block.removeAttribute("lang");
}
