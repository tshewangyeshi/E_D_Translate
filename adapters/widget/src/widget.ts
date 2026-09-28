// The dzweb widget (backlog S4.1).
// Requirements: FR-200, FR-201, FR-210, FR-214, FR-215, FR-216, FR-217, NFR-502.
//
//   <script src="https://…/dzweb.js" data-dz-site="portal" defer></script>
//
// No build step in the host page, no framework, no polyfills. One script tag.
//
// The ordering that matters, and why:
//
//   1. Fetch configuration FIRST. Without it the widget does not know which
//      regions are Tier 1 or private, so it cannot ask safely and offers no
//      toggle at all (FR-216).
//   2. Extract, translate, then write into existing text nodes only (FR-210).
//   3. Discard any answer that arrives after the page moved on (FR-217).
//
// Nothing here may throw into the host page. A government site that breaks
// because a translation widget failed is worse than an untranslated one, so
// every entry point is wrapped and every failure leaves the page in English.

import {
  applyAttribute,
  applyTranslation,
  clearLanguage,
  markLanguage,
  readSlots,
  restoreAttribute,
  restoreOriginal,
  unchangedSinceWrite,
  type AttributeState,
  type BlockState,
  type Registry,
} from "./apply.js";
import { extract, type AttributeUnit, type Extraction, type Segment } from "./extract.js";
import { stripRenderArtefacts } from "./locale-dz.js";
import { observe, type Observation } from "./observe.js";
import {
  fetchConfig,
  translateBatch,
  MAX_SEGMENTS,
  type ApiOptions,
  type SiteConfig,
} from "./api.js";

/** A reference that does not keep a removed block alive (spec ER-19). */
interface Ref<T extends object> {
  deref(): T | undefined;
}

/**
 * `WeakRef` where the engine has it, a strong reference where it does not.
 *
 * WeakRef is Chrome 84+, below the ~90 WebView floor this widget targets, so
 * the fallback should never run. It exists because `new WeakRef` on an engine
 * without it throws, and throwing into a government page to save memory is the
 * wrong trade (FR-215). The fallback keeps a block alive until the next toggle,
 * which is bounded and visible, rather than crashing.
 */
function makeRef<T extends object>(value: T): Ref<T> {
  return typeof WeakRef === "function" ? new WeakRef(value) : { deref: () => value };
}

/** How long to wait before asking again for segments the server queued. */
const PENDING_RETRY_MS = 8_000;

/** Per-origin preference key. Persistence proper is S4.3. */
const PREFERENCE_KEY = "dzweb.language";

export class Widget {
  private readonly registry: Registry = new WeakMap<Element, BlockState>();
  private config: SiteConfig | null = null;
  private showing: "en" | "dz" = "en";
  /** Bumped on every toggle, so responses from a previous state are dropped. */
  private epoch = 0;
  private pendingRetryDone = false;
  /** Blocks currently showing Dzongkha. Weak, so a removed block is collectable. */
  private translatedBlocks: Ref<Element>[] = [];
  /** Translated attribute values, per element then per attribute (FR-113). */
  private readonly attributes = new WeakMap<Element, Map<string, AttributeState>>();
  private translatedAttributes: Ref<Element>[] = [];
  private watching: Observation | null = null;

  constructor(
    private readonly api: ApiOptions,
    private readonly root: () => Element,
  ) {}

  /** Load configuration. False means: offer no toggle (FR-216). */
  async start(): Promise<boolean> {
    this.config = await fetchConfig(this.api);
    return this.config !== null;
  }

  get ready(): boolean {
    return this.config !== null;
  }

  get language(): "en" | "dz" {
    return this.showing;
  }

  /** Show Dzongkha. Safe to call twice. */
  async translate(): Promise<void> {
    if (this.config === null) return;
    this.showing = "dz";
    this.epoch += 1;
    this.pendingRetryDone = false;
    await this.pass(this.epoch);
  }

  /** Put the page back to English, honouring any host edit made meanwhile. */
  /**
   * Put the page back to English, honouring any host edit made meanwhile.
   *
   * This walks the blocks it translated rather than extracting again, because
   * a translated block carries `lang="dz…"` and extraction deliberately skips
   * those (FR-112). Re-extracting here would find nothing and silently leave
   * the page in Dzongkha.
   */
  toggleBack(): void {
    this.showing = "en";
    this.epoch += 1;
    for (const ref of this.translatedBlocks) {
      const block = ref.deref();
      if (block === undefined) continue; // the host removed it; nothing to restore
      const state = this.registry.get(block);
      if (state === undefined || !state.translated) continue;
      // Only restore if the block still holds what we wrote. If the host
      // changed it, its value is newer than our copy of the English (FR-212).
      if (unchangedSinceWrite(state.segment, state.lastWritten)) {
        restoreOriginal(state.segment, state.original);
      }
      clearLanguage(block);
      state.translated = false;
    }
    this.translatedBlocks = [];

    for (const ref of this.translatedAttributes) {
      const element = ref.deref();
      if (element === undefined || !element.isConnected) continue;
      const byAttr = this.attributes.get(element);
      if (byAttr === undefined) continue;
      for (const [attr, state] of byAttr) restoreAttribute(element, attr, state);
      this.attributes.delete(element);
    }
    this.translatedAttributes = [];
  }

  private async pass(epoch: number, retry = false, roots?: readonly Element[]): Promise<void> {
    const { segments, attributes } = this.extractUnits(roots);
    if (segments.length === 0 && attributes.length === 0) return;

    const sent = new Map<string, { segment: Segment; slots: string[]; generation: number }>();
    const items = segments.map((segment, n) => {
      const id = `s${n}`;
      const state = this.stateFor(segment);
      sent.set(id, { segment, slots: readSlots(segment), generation: state.generation });
      return {
        id,
        text: segment.text,
        selectorTier: segment.selectorTier,
        tierHint: segment.tierHint,
      };
    });

    // Attribute units carry no inline markup, so they need no slots -- but they
    // are translated in the same request, so alt text is not a second round trip.
    const sentAttrs = new Map<string, { unit: AttributeUnit; value: string }>();
    attributes.forEach((unit, n) => {
      const id = `a${n}`;
      sentAttrs.set(id, { unit, value: unit.text });
      items.push({ id, text: unit.text, selectorTier: null, tierHint: null });
    });

    const pending: string[] = [];
    for (let start = 0; start < items.length; start += MAX_SEGMENTS) {
      const batch = items.slice(start, start + MAX_SEGMENTS);
      const results = await translateBatch(this.api, location.pathname, batch);
      for (const [id, result] of results) {
        if (result.status === "pending_mt") pending.push(id);
        if (result.status !== "translated") continue; // English stays on the page
        const attr = sentAttrs.get(id);
        if (attr !== undefined) {
          this.writeAttribute(attr.unit, attr.value, result.text, epoch);
          continue;
        }
        const record = sent.get(id);
        if (record === undefined) continue;
        this.write(record, epoch, result.text, result.origin);
      }
    }

    // The server queued the rest; ask once more after it has had time (ER-3).
    if (pending.length > 0 && !retry && !this.pendingRetryDone) {
      this.pendingRetryDone = true;
      setTimeout(() => {
        if (this.epoch === epoch && this.showing === "dz") void this.pass(epoch, true);
      }, PENDING_RETRY_MS);
    }
  }

  /**
   * Apply one result, unless the page moved on while it was in flight.
   *
   * Three independent ways an answer can be stale, checked before the page is
   * touched: the reader toggled back or re-translated (epoch), the block was
   * re-extracted so these nodes may no longer be the live ones (generation),
   * or the host rewrote the text we asked about (node values).
   */
  private write(
    record: { segment: Segment; slots: string[]; generation: number },
    epoch: number,
    translated: string,
    origin: string | undefined,
  ): void {
    const { segment, slots, generation } = record;
    if (this.epoch !== epoch || this.showing !== "dz") return;
    const state = this.registry.get(segment.block);
    if (state === undefined || state.generation !== generation) return;
    if (!unchangedSinceWrite(segment, slots)) return;

    if (!applyTranslation(segment, translated)) return; // block stays English
    state.segment = segment;
    state.lastWritten = readSlots(segment).map(stripForCompare);
    state.translated = true;
    this.translatedBlocks.push(makeRef(segment.block));
    markLanguage(segment.block, origin);
  }

  /** Apply a translated attribute, unless the host changed it meanwhile. */
  private writeAttribute(
    unit: AttributeUnit,
    valueWhenSent: string,
    translated: string,
    epoch: number,
  ): void {
    if (this.epoch !== epoch || this.showing !== "dz") return;
    if (!applyAttribute(unit.element, unit.attr, translated, valueWhenSent)) return;
    let byAttr = this.attributes.get(unit.element);
    if (byAttr === undefined) {
      byAttr = new Map<string, AttributeState>();
      this.attributes.set(unit.element, byAttr);
    }
    byAttr.set(unit.attr, {
      original: valueWhenSent,
      lastWritten: stripRenderArtefacts(unit.element.getAttribute(unit.attr) ?? ""),
    });
    this.translatedAttributes.push(makeRef(unit.element));
  }

  /**
   * Start watching for content that arrives or changes after load (FR-211).
   *
   * Safe to call once configuration has loaded; stops with `unwatch()`.
   */
  watch(debounceMs?: number): void {
    if (this.watching !== null || this.config === null) return;
    this.watching = observe({
      root: this.root(),
      ...(debounceMs === undefined ? {} : { debounceMs }),
      blockOf: (node) => this.blockOf(node),
      isOwnWrite: (node) => this.isOwnWrite(node),
      onDirty: (blocks) => this.onDirty(blocks),
    });
  }

  unwatch(): void {
    this.watching?.stop();
    this.watching = null;
  }

  /** Report pending observer work immediately. Tests only. */
  flush(): void {
    this.watching?.flush();
  }

  /** The nearest element we would treat as a translation unit. */
  private blockOf(node: Node): Element | null {
    const element =
      node.nodeType === 1 ? (node as Element) : (node.parentElement ?? null);
    if (element === null || !element.isConnected) return null;
    if (element.closest("[data-dz-control]") !== null) return null; // our own UI
    // A known block wins; otherwise the element itself is the unit and
    // extraction will find the real block inside it.
    for (let at: Element | null = element; at !== null; at = at.parentElement) {
      if (this.registry.has(at)) return at;
    }
    return element;
  }

  /** True when this node still holds exactly what the widget last wrote to it. */
  private isOwnWrite(node: Text): boolean {
    const block = this.blockOf(node);
    if (block === null) return false;
    const state = this.registry.get(block);
    if (state === undefined || !state.translated) return false;
    return unchangedSinceWrite(state.segment, state.lastWritten);
  }

  /**
   * Handle blocks the host added or changed.
   *
   * A block we had translated and that no longer matches what we wrote has been
   * rewritten by the host. Its English is now whatever the host just put there,
   * so the remembered original is replaced rather than kept -- otherwise
   * toggling back would restore text the host has moved on from (FR-212).
   */
  private onDirty(blocks: Set<Element>): void {
    const affected: Element[] = [];
    for (const block of blocks) {
      if (!block.isConnected) continue;
      this.registry.delete(block); // its slots and original are both stale now
      affected.push(block);
    }
    if (affected.length === 0 || this.showing !== "dz") return;
    void this.pass(this.epoch, true, affected);
  }

  private stateFor(segment: Segment): BlockState {
    const existing = this.registry.get(segment.block);
    if (existing !== undefined) return existing;
    const state: BlockState = {
      segment,
      original: readSlots(segment),
      lastWritten: [],
      generation: 0,
      translated: false,
    };
    this.registry.set(segment.block, state);
    return state;
  }

  /**
   * Extract from the whole page, or from just the blocks that changed.
   *
   * A translated block carries `lang="dz…"` and extraction skips those by
   * design (FR-112), so a block being re-examined has its marker cleared first.
   * Otherwise a host edit inside translated content would be invisible.
   */
  private extractUnits(roots?: readonly Element[]): Extraction {
    const options = {
      tier1Selectors: this.config?.tier1Selectors ?? [],
      privateSelectors: this.config?.privateSelectors ?? [],
    };
    try {
      if (roots === undefined) return extract(this.root(), options);
      const segments: Segment[] = [];
      const attributes: AttributeUnit[] = [];
      for (const block of roots) {
        if (!block.isConnected) continue; // removed while we were debouncing
        clearLanguage(block);
        const found = extract(block, options);
        segments.push(...found.segments);
        attributes.push(...found.attributes);
      }
      return { segments, attributes };
    } catch {
      return { segments: [], attributes: [] };
    }
  }
}

/**
 * The comparison form of a rendered slot.
 *
 * `unchangedSinceWrite` strips render artefacts before comparing, so what we
 * remember writing must be stored stripped too, or every check would fail
 * against its own zero-width spaces.
 */
function stripForCompare(text: string): string {
  return stripRenderArtefacts(text);
}

/** Wait for load, then idle, then two quiet frames before auto-translating. */
export function whenQuiet(callback: () => void, win: Window = window): void {
  const afterFrames = (): void => {
    const raf = win.requestAnimationFrame?.bind(win);
    if (typeof raf !== "function") {
      callback();
      return;
    }
    raf(() => raf(() => callback()));
  };
  const afterIdle = (): void => {
    const idle = (win as unknown as { requestIdleCallback?: (cb: () => void) => void })
      .requestIdleCallback;
    if (typeof idle === "function") idle(afterFrames);
    else win.setTimeout(afterFrames, 0);
  };
  if (win.document.readyState === "complete") afterIdle();
  else win.addEventListener("load", afterIdle, { once: true });
}

/** Read the saved language preference, tolerating blocked storage. */
export function savedPreference(win: Window = window): "en" | "dz" | null {
  try {
    const value = win.localStorage.getItem(PREFERENCE_KEY);
    return value === "dz" || value === "en" ? value : null;
  } catch {
    return null;
  }
}

export function savePreference(value: "en" | "dz", win: Window = window): void {
  try {
    win.localStorage.setItem(PREFERENCE_KEY, value);
  } catch {
    /* private mode or blocked storage: the preference simply does not persist */
  }
}
