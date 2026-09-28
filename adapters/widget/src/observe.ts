// Watching the host page for content that appears or changes after load.
// Requirements: FR-211, FR-212 · [ER-11, ER-20]
//
// Two things must be watched, not one. `childList` catches nodes being added,
// which is what a server-rendered page or a jQuery widget does. `characterData`
// catches a value changing in place, which is what React and Vue do on a
// re-render -- they keep the node and write through it. Watching only childList
// means every framework update is invisible.
//
// The hard part is not noticing changes; it is ignoring our own. The widget
// writes to the page, which fires the observer, which would translate the
// translation. A re-entrancy flag is the obvious fix and the wrong one: the
// observer delivers records asynchronously, so by the time they arrive the flag
// is already back down and the writes look like the host's. Instead every
// change is checked against what the widget last wrote to that node. Our own
// writes match and are dropped; anything else is the host and is real.

/** A change the host made, grouped by the block it happened in. */
export interface ObserveOptions {
  root: Element;
  /** Nearest translatable block for a changed node, or null to ignore it. */
  blockOf: (node: Node) => Element | null;
  /** True when this text node holds exactly what the widget last wrote. */
  isOwnWrite: (node: Text) => boolean;
  /** Called with the affected blocks after the page stops changing. */
  onDirty: (blocks: Set<Element>) => void;
  /** Quiet period before reporting. One SPA route change should be one batch. */
  debounceMs?: number;
  window?: Window;
}

export const DEFAULT_DEBOUNCE_MS = 150;

export interface Observation {
  stop(): void;
  /** Report immediately instead of waiting out the debounce (for tests). */
  flush(): void;
}

export function observe(options: ObserveOptions): Observation {
  const win = options.window ?? window;
  const debounceMs = options.debounceMs ?? DEFAULT_DEBOUNCE_MS;
  const dirty = new Set<Element>();
  let timer: ReturnType<Window["setTimeout"]> | null = null;

  const report = (): void => {
    timer = null;
    if (dirty.size === 0) return;
    const batch = new Set(dirty);
    dirty.clear();
    options.onDirty(batch);
  };

  const schedule = (): void => {
    if (timer !== null) win.clearTimeout(timer);
    timer = win.setTimeout(report, debounceMs);
  };

  const note = (node: Node): void => {
    const block = options.blockOf(node);
    if (block !== null) dirty.add(block);
  };

  const observer = new MutationObserver((records) => {
    let sawSomething = false;
    for (const record of records) {
      if (record.type === "characterData") {
        const node = record.target as Text;
        if (options.isOwnWrite(node)) continue; // our own write, not the host's
        note(node);
        sawSomething = true;
        continue;
      }
      for (const added of Array.from(record.addedNodes)) {
        note(added);
        sawSomething = true;
      }
      // Removals matter only because a block may now be empty; the block
      // itself is still the unit, so note where it happened.
      if (record.removedNodes.length > 0 && record.target !== null) {
        note(record.target);
        sawSomething = true;
      }
    }
    if (sawSomething) schedule();
  });

  observer.observe(options.root, {
    childList: true,
    subtree: true,
    characterData: true,
  });

  return {
    stop(): void {
      if (timer !== null) win.clearTimeout(timer);
      observer.disconnect();
    },
    flush(): void {
      if (timer !== null) win.clearTimeout(timer);
      report();
    },
  };
}
