// @vitest-environment jsdom
// S4.1 — widget orchestration.
// Requirements: FR-200, FR-210, FR-214, FR-215, FR-216, FR-217, FR-155.
//
// The theme is refusal. Almost every test here checks that the widget declines
// to touch the page: no configuration, a failed request, an answer that arrived
// after the reader toggled away, an answer about text the host has since
// rewritten. A translation widget that guesses in those moments corrupts a
// government page, so guessing is the bug.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Widget, savedPreference, savePreference, whenQuiet } from "../src/widget";
import type { ApiOptions } from "../src/api";

interface Reply {
  status?: number;
  body?: unknown;
  reject?: boolean;
}

/** A fetch stand-in that answers /v1/config and /v1/translate from a script. */
function fakeFetch(replies: { config?: Reply; translate?: Reply[] }): {
  impl: typeof fetch;
  calls: { url: string; body: unknown }[];
} {
  const calls: { url: string; body: unknown }[] = [];
  let translateCall = 0;
  const impl = (async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });
    const isConfig = url.includes("/v1/config");
    const reply = isConfig
      ? (replies.config ?? { body: defaultConfig })
      : (replies.translate?.[Math.min(translateCall++, (replies.translate?.length ?? 1) - 1)] ?? {
          body: { segments: [] },
        });
    if (reply.reject) throw new TypeError("network error");
    return {
      ok: (reply.status ?? 200) < 400,
      status: reply.status ?? 200,
      json: async () => reply.body,
    } as Response;
  }) as unknown as typeof fetch;
  return { impl, calls };
}

const defaultConfig = {
  site: "portal",
  default_tier: 2,
  tier1_selectors: [".legal"],
  private_selectors: [],
};

function options(impl: typeof fetch): ApiOptions {
  return { base: "https://api.example", site: "portal", fetchImpl: impl };
}

function makeWidget(replies: Parameters<typeof fakeFetch>[0]): {
  widget: Widget;
  calls: { url: string; body: unknown }[];
} {
  const { impl, calls } = fakeFetch(replies);
  return { widget: new Widget(options(impl), () => document.body), calls };
}

function bodyText(): string {
  return document.body.textContent ?? "";
}

beforeEach(() => {
  document.body.innerHTML = "<p>Apply for a passport.</p>";
});

describe("configuration gate (FR-216)", () => {
  it("fr216_offers_nothing_when_configuration_is_unavailable", async () => {
    const { widget } = makeWidget({ config: { status: 503 } });
    expect(await widget.start()).toBe(false);
    expect(widget.ready).toBe(false);

    await widget.translate(); // must be a no-op, not a crash
    expect(bodyText()).toBe("Apply for a passport.");
  });

  it("fr216_survives_a_network_failure_fetching_configuration", async () => {
    const { widget } = makeWidget({ config: { reject: true } });
    expect(await widget.start()).toBe(false);
  });

  it("fr216_survives_nonsense_configuration", async () => {
    const { widget } = makeWidget({ config: { body: { unexpected: true } } });
    expect(await widget.start()).toBe(false);
  });

  it("fr216_is_ready_with_valid_configuration", async () => {
    const { widget } = makeWidget({});
    expect(await widget.start()).toBe(true);
    expect(widget.ready).toBe(true);
  });
});

describe("translating (FR-210, FR-214)", () => {
  it("fr210_writes_the_translation_into_the_page", async () => {
    const { widget } = makeWidget({
      translate: [{ body: { segments: [{ id: "s0", text: "DZ-text", status: "translated", origin: "mt" }] } }],
    });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("DZ-text");
    expect(document.querySelector("p")?.getAttribute("lang")).toBe("dz-x-mtfrom-en");
  });

  it("fr510_leaves_tier_blocked_segments_in_english", async () => {
    const { widget } = makeWidget({
      translate: [
        { body: { segments: [{ id: "s0", text: "Apply for a passport.", status: "tier_blocked" }] } },
      ],
    });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Apply for a passport.");
    expect(document.querySelector("p")?.hasAttribute("lang")).toBe(false);
  });

  it("fr212_toggling_back_restores_the_english", async () => {
    const { widget } = makeWidget({
      translate: [{ body: { segments: [{ id: "s0", text: "DZ-text", status: "translated" }] } }],
    });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("DZ-text");

    widget.toggleBack();
    expect(bodyText()).toBe("Apply for a passport.");
    expect(document.querySelector("p")?.hasAttribute("lang")).toBe(false);
    expect(widget.language).toBe("en");
  });

  it("fr212_leaves_a_host_edit_alone_when_toggling_back", async () => {
    const { widget } = makeWidget({
      translate: [{ body: { segments: [{ id: "s0", text: "DZ-text", status: "translated" }] } }],
    });
    await widget.start();
    await widget.translate();

    // The host re-rendered while Dzongkha was showing. Its value is newer than
    // our English, so restoring would throw away the host's own update.
    (document.querySelector("p") as HTMLElement).firstChild!.nodeValue = "Host wrote this";
    widget.toggleBack();
    expect(bodyText()).toBe("Host wrote this");
  });
});

describe("failure safety (FR-215)", () => {
  it("fr215_a_failed_translate_leaves_the_page_in_english", async () => {
    const { widget } = makeWidget({ translate: [{ reject: true }] });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Apply for a passport.");
  });

  it("fr215_a_500_leaves_the_page_in_english", async () => {
    const { widget } = makeWidget({ translate: [{ status: 500 }] });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Apply for a passport.");
  });

  it("fr215_nonsense_json_leaves_the_page_in_english", async () => {
    const { widget } = makeWidget({ translate: [{ body: { segments: "not a list" } }] });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Apply for a passport.");
  });

  it("fr215_an_unusable_translation_leaves_that_block_in_english", async () => {
    document.body.innerHTML = "<p>Click <a href='/x'>here</a> now.</p>";
    const { widget } = makeWidget({
      translate: [{ body: { segments: [{ id: "s0", text: "no markers at all", status: "translated" }] } }],
    });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Click here now.");
  });
});

describe("stale responses (FR-217)", () => {
  it("fr217_discards_a_response_that_lands_after_toggling_back", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const impl = (async (url: string) => {
      if (url.includes("/v1/config")) {
        return { ok: true, status: 200, json: async () => defaultConfig } as Response;
      }
      await gate;
      return {
        ok: true,
        status: 200,
        json: async () => ({ segments: [{ id: "s0", text: "DZ-late", status: "translated" }] }),
      } as Response;
    }) as unknown as typeof fetch;

    const widget = new Widget(options(impl), () => document.body);
    await widget.start();
    const inFlight = widget.translate();
    widget.toggleBack(); // reader changed their mind
    release!();
    await inFlight;

    expect(bodyText()).toBe("Apply for a passport.");
  });

  it("fr217_discards_a_response_about_text_the_host_has_rewritten", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const impl = (async (url: string) => {
      if (url.includes("/v1/config")) {
        return { ok: true, status: 200, json: async () => defaultConfig } as Response;
      }
      await gate;
      return {
        ok: true,
        status: 200,
        json: async () => ({ segments: [{ id: "s0", text: "DZ-stale", status: "translated" }] }),
      } as Response;
    }) as unknown as typeof fetch;

    const widget = new Widget(options(impl), () => document.body);
    await widget.start();
    const inFlight = widget.translate();
    (document.querySelector("p") as HTMLElement).firstChild!.nodeValue = "Host replaced this";
    release!();
    await inFlight;

    expect(bodyText()).toBe("Host replaced this");
  });
});

describe("queued work (FR-155)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fr155_asks_again_once_for_segments_the_server_queued", async () => {
    vi.useFakeTimers();
    const { widget, calls } = makeWidget({
      translate: [
        { body: { segments: [{ id: "s0", text: "Apply for a passport.", status: "pending_mt" }] } },
        { body: { segments: [{ id: "s0", text: "DZ-ready", status: "translated" }] } },
      ],
    });
    await widget.start();
    await widget.translate();
    expect(bodyText()).toBe("Apply for a passport."); // still English while queued

    await vi.advanceTimersByTimeAsync(8_100);
    expect(bodyText()).toBe("DZ-ready");

    const translateCalls = calls.filter((c) => c.url.includes("/v1/translate"));
    expect(translateCalls.length).toBe(2); // once more, not a poll
  });

  it("fr155_does_not_retry_after_the_reader_toggled_back", async () => {
    vi.useFakeTimers();
    const { widget, calls } = makeWidget({
      translate: [
        { body: { segments: [{ id: "s0", text: "Apply for a passport.", status: "pending_mt" }] } },
      ],
    });
    await widget.start();
    await widget.translate();
    widget.toggleBack();

    await vi.advanceTimersByTimeAsync(8_100);
    expect(calls.filter((c) => c.url.includes("/v1/translate")).length).toBe(1);
  });
});

describe("batching (FR-100)", () => {
  it("fr100_splits_more_than_64_blocks_across_requests", async () => {
    document.body.innerHTML = Array.from({ length: 70 }, (_, n) => `<p>Line ${n} text.</p>`).join("");
    const { widget, calls } = makeWidget({});
    await widget.start();
    await widget.translate();

    const batches = calls
      .filter((c) => c.url.includes("/v1/translate"))
      .map((c) => (c.body as { segments: unknown[] }).segments.length);
    expect(batches.length).toBe(2);
    expect(batches[0]).toBe(64);
    expect(batches.reduce((a, b) => a + b, 0)).toBe(70);
  });
});

describe("auto-translate timing and preference", () => {
  it("nfr502_waits_for_load_then_idle_then_two_frames", async () => {
    const order: string[] = [];
    let frames = 0;
    const win = {
      document: { readyState: "complete" },
      requestAnimationFrame: (cb: () => void) => {
        frames += 1;
        order.push(`frame${frames}`);
        cb();
        return frames;
      },
      requestIdleCallback: (cb: () => void) => {
        order.push("idle");
        cb();
      },
      setTimeout: (cb: () => void) => {
        cb();
        return 0;
      },
      addEventListener: () => undefined,
    } as unknown as Window;

    whenQuiet(() => order.push("translate"), win);
    expect(order).toEqual(["idle", "frame1", "frame2", "translate"]);
  });

  it("nfr502_waits_for_load_when_the_document_is_still_loading", () => {
    let loadHandler: (() => void) | null = null;
    const order: string[] = [];
    const win = {
      document: { readyState: "loading" },
      requestAnimationFrame: (cb: () => void) => {
        cb();
        return 1;
      },
      setTimeout: (cb: () => void) => {
        cb();
        return 0;
      },
      addEventListener: (_: string, handler: () => void) => {
        loadHandler = handler;
      },
    } as unknown as Window;

    whenQuiet(() => order.push("translate"), win);
    expect(order).toEqual([]); // nothing before load
    loadHandler!();
    expect(order).toEqual(["translate"]);
  });

  it("fr213_a_blocked_storage_does_not_throw", () => {
    const win = {
      localStorage: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      },
    } as unknown as Window;
    expect(savedPreference(win)).toBe(null);
    expect(() => savePreference("dz", win)).not.toThrow();
  });

  it("fr213_round_trips_a_preference", () => {
    savePreference("dz", window);
    expect(savedPreference(window)).toBe("dz");
    savePreference("en", window);
    expect(savedPreference(window)).toBe("en");
  });
});
