// Export translatable segments from saved HTML pages with the REAL widget extractor
// (S2.4 pre-warm; the same code the browser runs). Output feeds
// `python -m orchestrator.ops.prewarm`.
//
//   npm run build
//   node scripts/export-segments.mjs --tier1 ".fees" page1.html=/services/renewal > segments.json
//
// Each page is given the path it is served at on the real site, after '='. The
// server tiers content by path (FR-512), so a snapshot without one cannot be
// tiered and pre-warm will refuse it rather than guess.
//
// Snapshots must be PUBLIC pages with synthetic data only (docs/CLAUDE.md).
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { extract } from "../dist/extract.js";

const args = process.argv.slice(2);
const tier1Selectors = [];
const privateSelectors = [];
const files = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--tier1") tier1Selectors.push(args[++i]);
  else if (args[i] === "--private") privateSelectors.push(args[++i]);
  else files.push(args[i]);
}
if (files.length === 0) {
  console.error("usage: export-segments.mjs [--tier1 SEL]... [--private SEL]... page.html[=/site/path]...");
  process.exit(2);
}

const pages = files.map((arg) => {
  // Split on the last '=' so Windows paths ("C:\snapshots\a.html=/x") survive.
  const split = arg.lastIndexOf("=");
  const file = split === -1 ? arg : arg.slice(0, split);
  const path = split === -1 ? null : arg.slice(split + 1);
  if (path !== null && !path.startsWith("/")) {
    console.error(`page path must start with '/': ${path}`);
    process.exit(2);
  }
  const dom = new JSDOM(readFileSync(file, "utf8"));
  const doc = dom.window.document;
  if (doc.documentElement.hasAttribute("data-dz-private")) {
    return { file, path, skipped: "data-dz-private", segments: [] };
  }
  const { segments } = extract(doc.body, { tier1Selectors, privateSelectors });
  return {
    file,
    path,
    segments: segments.map((s) => ({ text: s.text, tier: s.tierHint, selector_tier: s.selectorTier })),
  };
});
process.stdout.write(JSON.stringify({ pages }, null, 2) + "\n");
