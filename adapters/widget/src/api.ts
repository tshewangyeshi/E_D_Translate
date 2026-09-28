// Talking to the orchestrator (FR-100, FR-103, FR-215, FR-216).
//
// Every call here resolves rather than rejects. The widget's contract with the
// host page is that a failure leaves the page in English with no uncaught
// exception, so a network error, a 500, a 403 or nonsense JSON all arrive as
// "no result" and the caller does nothing.

/** Batch limit the server enforces (FR-100); exceeding it is a 413. */
export const MAX_SEGMENTS = 64;

export interface SiteConfig {
  site: string;
  defaultTier: number;
  tier1Selectors: string[];
  privateSelectors: string[];
}

export type SegmentStatus =
  | "translated"
  | "pending_mt"
  | "tier_blocked"
  | "entity_check_failed"
  | "tag_fallback"
  | "glossary_term_missing";

export interface SegmentResult {
  id: string;
  text: string;
  status: SegmentStatus;
  origin?: string;
  /** Hash of the masked source, for reporting an error against it (FR-104). */
  segmentKey?: string;
}

export interface ApiOptions {
  base: string;
  site: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 10_000;

async function request(
  options: ApiOptions,
  path: string,
  init: RequestInit,
): Promise<unknown | null> {
  const doFetch = options.fetchImpl ?? globalThis.fetch;
  if (typeof doFetch !== "function") return null;
  // AbortSignal.timeout is not in every supported WebView; fall back silently.
  const signal =
    typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TIMEOUT_MS)
      : undefined;
  try {
    const response = await doFetch(`${options.base}${path}`, { ...init, signal });
    if (!response.ok) return null;
    return (await response.json()) as unknown;
  } catch {
    return null; // network error, abort, or invalid JSON: the page stays English
  }
}

/** Site configuration, or null. Null means the widget offers no toggle (FR-216). */
export async function fetchConfig(options: ApiOptions): Promise<SiteConfig | null> {
  const body = await request(options, `/v1/config?site=${encodeURIComponent(options.site)}`, {
    method: "GET",
  });
  if (body === null || typeof body !== "object") return null;
  const raw = body as Record<string, unknown>;
  if (typeof raw["site"] !== "string") return null;
  return {
    site: raw["site"],
    defaultTier: typeof raw["default_tier"] === "number" ? raw["default_tier"] : 1,
    tier1Selectors: stringList(raw["tier1_selectors"]),
    privateSelectors: stringList(raw["private_selectors"]),
  };
}

export interface TranslateItem {
  id: string;
  text: string;
  selectorTier: 1 | null;
  tierHint: number | null;
}

/** Translate one batch. Returns results by id, or an empty map on any failure. */
export async function translateBatch(
  options: ApiOptions,
  path: string,
  items: readonly TranslateItem[],
): Promise<Map<string, SegmentResult>> {
  const out = new Map<string, SegmentResult>();
  if (items.length === 0) return out;
  const body = await request(options, "/v1/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      site: options.site,
      path,
      segments: items.map((item) => ({
        id: item.id,
        text: item.text,
        // Only ever sent to make content stricter; the server decides (FR-512).
        ...(item.selectorTier === 1 || item.tierHint !== null
          ? { selector_tier: item.selectorTier ?? item.tierHint }
          : {}),
      })),
    }),
  });
  if (body === null || typeof body !== "object") return out;
  const segments = (body as Record<string, unknown>)["segments"];
  if (!Array.isArray(segments)) return out;
  for (const entry of segments) {
    if (typeof entry !== "object" || entry === null) continue;
    const raw = entry as Record<string, unknown>;
    if (typeof raw["id"] !== "string" || typeof raw["text"] !== "string") continue;
    out.set(raw["id"], {
      id: raw["id"],
      text: raw["text"],
      status: (raw["status"] as SegmentStatus) ?? "tag_fallback",
      ...(typeof raw["origin"] === "string" ? { origin: raw["origin"] } : {}),
      ...(typeof raw["segment_key"] === "string" ? { segmentKey: raw["segment_key"] } : {}),
    });
  }
  return out;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/**
 * Report a translation error (FR-430).
 *
 * Resolves true when the server accepted the request, which is NOT the same as
 * the report being kept: 202 is returned whether it was stored, rate-limited,
 * saturated or dropped as a honeypot hit, deliberately. The widget cannot know
 * which, and must not pretend otherwise.
 *
 * It does not go through `request`, which parses JSON: a 202 has no body, so
 * parsing would throw and a successful report would look like a failure.
 */
export async function sendFeedback(
  options: ApiOptions,
  segmentKey: string,
  reason: string,
  comment: string,
): Promise<boolean> {
  const doFetch = options.fetchImpl ?? globalThis.fetch;
  if (typeof doFetch !== "function") return false;
  try {
    const response = await doFetch(`${options.base}/v1/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site: options.site,
        segment_key: segmentKey,
        reason,
        ...(comment ? { comment } : {}),
      }),
    });
    return response.ok;
  } catch {
    return false; // network failure: the reader is thanked either way
  }
}
