// S6.2 — line-break assistance, the widget's half.
// Requirements: FR-160 · [ER-8]
//
// The server and the widget must insert break opportunities at the same
// places. One fixture, read by both suites (tests/orchestrator/test_linebreak.py
// is the other half), so they cannot drift.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { insertBreaks, stripRenderArtefacts } from "../src/locale-dz";

const { cases } = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../tests/fixtures/linebreak/cases.json"), "utf8"),
) as { cases: { name: string; input: string; rendered: string }[] };

describe("line breaks (FR-160)", () => {
  it.each(cases)("fr160_breaks_go_where_the_shared_fixture_says: $name", ({ input, rendered }) => {
    expect(insertBreaks(input)).toBe(rendered);
  });

  it.each(cases)("fr160_stripping_recovers_the_stored_text: $name", ({ input, rendered }) => {
    expect(stripRenderArtefacts(rendered)).toBe(stripRenderArtefacts(input));
  });
});
