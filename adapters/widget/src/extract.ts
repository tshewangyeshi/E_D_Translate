// Block extraction and wire encoding for the dzweb widget.
// Requirements: FR-110, FR-111, FR-112, FR-113, FR-120, FR-121, FR-500, FR-512, NFR-304.
//
// A segment is a BLOCK (nearest non-inline element), never a single text
// node, so verb-final Dzongkha can reorder words across inline markup.
// Inline elements become marker pairs; everything between markers is a SLOT
// backed by zero or more existing Text nodes. Translations are written back
// slot by slot into those same nodes (FR-210): nothing is ever created,
// removed or moved in the host page.
//
//   <p>Click <a><b>here</b></a> to apply.</p>
//        │         │               │
//   slot0 "Click " │ slot1 "here"  │ slot2 " to apply."
//   text: "Click ⟦1⟧here⟦/1⟧ to apply."
//
// Nested inlines collapse into their outermost pair (spec §2.3 default):
// all text nodes under <a> form one slot.

export type Tier = 1 | 2 | 3;

export interface ExtractOptions {
  /** CSS selectors the site marks Tier 1 (from GET /v1/config). */
  tier1Selectors?: readonly string[];
  /** CSS selectors for regions holding personal data (NFR-304). */
  privateSelectors?: readonly string[];
}

export interface Segment {
  /** The block element this segment belongs to. */
  block: Element;
  /** Wire-encoded text: escaped source with ⟦N⟧…⟦/N⟧ and ⟦vN/⟧ markers. */
  text: string;
  /** Text nodes per slot, in document order. An empty array is a slot-less position. */
  slots: Text[][];
  /** data-dz-tier of the nearest ancestor, if it is 1, 2 or 3 (a hint only, FR-512). */
  tierHint: Tier | null;
  /** 1 when the block sits inside a configured Tier 1 selector. */
  selectorTier: 1 | null;
}

export interface AttributeUnit {
  element: Element;
  attr: "alt" | "title" | "placeholder" | "value";
  text: string;
}

export interface Extraction {
  segments: Segment[];
  attributes: AttributeUnit[];
}

/** Never rendered as page text: ignored entirely, no marker, no boundary. */
const NON_RENDERED = new Set(["script", "style", "noscript", "template"]);

/** Content that must never reach the model (FR-110). */
const SKIP_TAGS = new Set([
  "textarea", "code", "pre", "kbd", "samp", "var", "svg", "math", "select", "option", "iframe", "object",
]);

/** Phrasing elements that stay inside a sentence. */
const INLINE_TAGS = new Set([
  "a", "abbr", "b", "bdi", "bdo", "cite", "data", "dfn", "em", "font", "i", "mark", "q",
  "s", "small", "span", "strong", "sub", "sup", "time", "u",
]);

/** Inline elements with no text of their own: they become ⟦vN/⟧ markers. */
const VOID_TAGS = new Set(["br", "img", "wbr", "input"]);

/** Skipped tags that sit inside a sentence become opaque ⟦vN/⟧ markers. */
const INLINE_SKIP_TAGS = new Set(["code", "kbd", "samp", "var", "select", "textarea"]);

const OPT_OUT_CLASSES = ["notranslate", "skiptranslate"];

/** Order within one element; aria-label and aria-description are never translated (FR-113). */
const TRANSLATABLE_ATTRS = ["alt", "title", "placeholder"] as const;

const LETTER = /\p{L}/u;

function tagOf(el: Element): string {
  return el.localName.toLowerCase();
}

function escapeWire(text: string): string {
  return text.split("⟦").join("⟦⟦").split("⟧").join("⟧⟧");
}

function matchesAny(el: Element, selectors: readonly string[]): boolean {
  for (const selector of selectors) {
    try {
      if (el.matches(selector)) return true;
    } catch {
      // A malformed selector from config must never break the host page (NFR-410).
    }
  }
  return false;
}

function closestAny(el: Element, selectors: readonly string[]): boolean {
  for (let e: Element | null = el; e; e = e.parentElement) {
    if (matchesAny(e, selectors)) return true;
  }
  return false;
}

class Extractor {
  private readonly segments: Segment[] = [];
  private readonly tier1Selectors: readonly string[];
  private readonly privateSelectors: readonly string[];

  constructor(options: ExtractOptions) {
    this.tier1Selectors = options.tier1Selectors ?? [];
    this.privateSelectors = options.privateSelectors ?? [];
  }

  /** Element (and all its descendants) must not be translated (FR-110, FR-111, FR-112, NFR-304). */
  isExcluded(el: Element): boolean {
    const tag = tagOf(el);
    if (SKIP_TAGS.has(tag) || NON_RENDERED.has(tag)) return true;
    if (el.getAttribute("translate") === "no") return true;
    if (el.hasAttribute("data-no-translate") || el.hasAttribute("data-dz-skip")) return true;
    if (el.hasAttribute("hidden")) return true;
    const lang = el.getAttribute("lang");
    if (lang !== null && lang.toLowerCase().startsWith("dz")) return true;
    for (const cls of OPT_OUT_CLASSES) {
      if (el.classList.contains(cls)) return true;
    }
    return matchesAny(el, this.privateSelectors);
  }

  isExcludedDeep(el: Element, root: Element): boolean {
    for (let e: Element | null = el; e; e = e.parentElement) {
      if (this.isExcluded(e)) return true;
      if (e === root) break;
    }
    return false;
  }

  /** An inline element that wraps block content is treated as a block container. */
  private containsBlock(el: Element): boolean {
    for (const child of Array.from(el.children)) {
      const tag = tagOf(child);
      if (NON_RENDERED.has(tag)) continue;
      if (!INLINE_TAGS.has(tag) && !VOID_TAGS.has(tag) && !INLINE_SKIP_TAGS.has(tag)) return true;
      if (this.containsBlock(child)) return true;
    }
    return false;
  }

  private collapsedTextNodes(el: Element, out: Text[]): void {
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === 3) {
        out.push(child as Text);
      } else if (child.nodeType === 1 && !this.isExcluded(child as Element)) {
        this.collapsedTextNodes(child as Element, out);
      }
    }
  }

  processBlock(block: Element): void {
    let enc = new Encoder();
    const flush = (): void => {
      const seg = enc.finish(block, this.tierHintOf(block), this.selectorTierOf(block));
      if (seg) this.segments.push(seg);
      enc = new Encoder();
    };

    for (const child of Array.from(block.childNodes)) {
      if (child.nodeType === 3) {
        enc.addText(child as Text);
        continue;
      }
      if (child.nodeType !== 1) continue;
      const el = child as Element;
      const tag = tagOf(el);
      if (NON_RENDERED.has(tag)) continue;

      if (this.isExcluded(el)) {
        if (INLINE_TAGS.has(tag) || VOID_TAGS.has(tag) || INLINE_SKIP_TAGS.has(tag)) {
          enc.addVoid(); // opaque: the model never sees its content
        } else {
          flush(); // an excluded block ends the current stretch
        }
        continue;
      }

      if (VOID_TAGS.has(tag)) {
        enc.addVoid();
      } else if (INLINE_TAGS.has(tag) && !this.containsBlock(el)) {
        const nodes: Text[] = [];
        this.collapsedTextNodes(el, nodes);
        enc.addInline(nodes);
      } else {
        flush();
        this.processBlock(el); // innermost block wins
      }
    }
    flush();
  }

  private tierHintOf(block: Element): Tier | null {
    const holder = block.closest("[data-dz-tier]");
    if (!holder) return null;
    const value = holder.getAttribute("data-dz-tier");
    return value === "1" ? 1 : value === "2" ? 2 : value === "3" ? 3 : null;
  }

  private selectorTierOf(block: Element): 1 | null {
    return closestAny(block, this.tier1Selectors) ? 1 : null;
  }

  result(): Segment[] {
    return this.segments;
  }
}

class Encoder {
  private readonly parts: string[] = [];
  private readonly slots: Text[][] = [[]];
  private counter = 0;

  addText(node: Text): void {
    this.lastSlot().push(node);
    this.parts.push(escapeWire(node.nodeValue ?? ""));
  }

  addVoid(): void {
    this.counter += 1;
    this.parts.push(`⟦v${this.counter}/⟧`);
    this.slots.push([]);
  }

  addInline(nodes: Text[]): void {
    this.counter += 1;
    const n = this.counter;
    this.parts.push(`⟦${n}⟧`);
    this.slots.push(nodes);
    for (const node of nodes) this.parts.push(escapeWire(node.nodeValue ?? ""));
    this.parts.push(`⟦/${n}⟧`);
    this.slots.push([]);
  }

  finish(block: Element, tierHint: Tier | null, selectorTier: 1 | null): Segment | null {
    const raw = this.slots.map((s) => s.map((n) => n.nodeValue ?? "").join("")).join("");
    if (!LETTER.test(raw)) return null; // whitespace, punctuation or digits only (FR-112)
    return { block, text: this.parts.join(""), slots: this.slots, tierHint, selectorTier };
  }

  private lastSlot(): Text[] {
    return this.slots[this.slots.length - 1] as Text[];
  }
}

function extractAttributes(root: Element, ex: Extractor): AttributeUnit[] {
  const units: AttributeUnit[] = [];
  const consider = (el: Element): void => {
    if (ex.isExcludedDeep(el, root)) return;
    for (const attr of TRANSLATABLE_ATTRS) {
      const text = el.getAttribute(attr);
      if (text && LETTER.test(text)) units.push({ element: el, attr, text });
    }
    if (tagOf(el) === "input") {
      const type = (el.getAttribute("type") ?? "").toLowerCase();
      const text = el.getAttribute("value");
      if ((type === "submit" || type === "button") && text && LETTER.test(text)) {
        units.push({ element: el, attr: "value", text });
      }
    }
  };
  consider(root);
  for (const el of Array.from(root.querySelectorAll("*"))) consider(el);
  return units;
}

/** Extract translatable segments and attribute units under `root`, in document order. */
export function extract(root: Element, options: ExtractOptions = {}): Extraction {
  const ex = new Extractor(options);
  if (!ex.isExcludedDeep(root, root)) ex.processBlock(root);
  return { segments: ex.result(), attributes: extractAttributes(root, ex) };
}

/** Current text of each slot, concatenating adjacent text nodes. */
export function slotTexts(segment: Segment): string[] {
  return segment.slots.map((nodes) => nodes.map((n) => n.nodeValue ?? "").join(""));
}
