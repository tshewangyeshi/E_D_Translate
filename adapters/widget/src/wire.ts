// Decoding the wire format the server returns (FR-121, FR-122).
//
// `extract.ts` encodes a block into markers plus slots; this reverses that for
// the model's answer. The two must agree exactly on the marker sequence, so a
// single decoder produces both sides and they are compared as sequences rather
// than as counts: ⟦1⟧⟦/1⟧⟦2⟧⟦/2⟧ and ⟦2⟧⟦/2⟧⟦1⟧⟦/1⟧ hold the same markers in a
// different order, and writing the second into the first would move the link
// onto the wrong words.
//
// Decoding is deliberately strict. Anything unexpected returns null and the
// caller leaves the block in English; a partially understood translation is
// worse than none.

/** One slot of text plus the marker sequence around it. */
export interface Decoded {
  /** Text per slot, in the same order `extract.ts` built its slots. */
  slots: string[];
  /** Markers in document order: "o1" open, "c1" close, "v1" void. */
  markers: string[];
}

const OPEN = /^⟦(\d+)⟧/;
const CLOSE = /^⟦\/(\d+)⟧/;
const VOID = /^⟦v(\d+)\/⟧/;

/** Decode wire text, or null when it is malformed. */
export function decodeWire(text: string): Decoded | null {
  const slots: string[] = [];
  const markers: string[] = [];
  let current = "";
  let rest = text;
  const open: number[] = [];

  while (rest.length > 0) {
    if (rest.startsWith("⟦⟦")) {
      current += "⟦";
      rest = rest.slice(2);
      continue;
    }
    if (rest.startsWith("⟧⟧")) {
      current += "⟧";
      rest = rest.slice(2);
      continue;
    }
    if (rest.startsWith("⟦")) {
      const voidMatch = VOID.exec(rest);
      if (voidMatch) {
        markers.push(`v${voidMatch[1]}`);
        slots.push(current);
        current = "";
        rest = rest.slice(voidMatch[0].length);
        continue;
      }
      const closeMatch = CLOSE.exec(rest);
      if (closeMatch) {
        const n = Number(closeMatch[1]);
        if (open.pop() !== n) return null; // unbalanced or crossed pairs
        markers.push(`c${n}`);
        slots.push(current);
        current = "";
        rest = rest.slice(closeMatch[0].length);
        continue;
      }
      const openMatch = OPEN.exec(rest);
      if (openMatch) {
        const n = Number(openMatch[1]);
        if (open.length > 0) return null; // extract.ts never nests pairs
        open.push(n);
        markers.push(`o${n}`);
        slots.push(current);
        current = "";
        rest = rest.slice(openMatch[0].length);
        continue;
      }
      return null; // a lone ⟦ is not a marker we produced
    }
    if (rest.startsWith("⟧")) return null; // unescaped closing bracket
    const next = rest.search(/[⟦⟧]/);
    if (next === -1) {
      current += rest;
      rest = "";
    } else {
      current += rest.slice(0, next);
      rest = rest.slice(next);
    }
  }

  if (open.length > 0) return null; // unclosed pair
  slots.push(current);
  return { slots, markers };
}

/** True when two decodings carry the same markers in the same order. */
export function sameMarkers(a: Decoded, b: Decoded): boolean {
  if (a.markers.length !== b.markers.length) return false;
  return a.markers.every((marker, n) => marker === b.markers[n]);
}
