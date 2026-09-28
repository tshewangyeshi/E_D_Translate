// Build the widget: tsc -> minify -> hash -> SRI (backlog S4.1, FR-200, FR-201).
//
//   node scripts/build.mjs [--check]
//
// No bundler, by decision of the engineering review: tsc emits ES modules and
// esbuild is used only to minify each one. The browser resolves the imports, so
// the shipped artefact is a module graph rather than one file, and the 15 KB
// budget is measured over the WHOLE graph gzipped -- what a first-time visitor
// actually downloads -- not over the entry alone.
//
// --check exits non-zero when the budget is exceeded, for CI.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { gzipSync } from "node:zlib";
import { readdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const DIST = join(ROOT, "dist");
const ENTRY = "main.js";
const BUDGET_BYTES = 15 * 1024; // FR-201

function run(command, args) {
  execFileSync(command, args, { cwd: ROOT, stdio: "inherit", shell: process.platform === "win32" });
}

function jsFiles() {
  return readdirSync(DIST).filter((name) => name.endsWith(".js")).sort();
}

rmSync(DIST, { recursive: true, force: true });
run("npx", ["tsc", "-p", "tsconfig.build.json"]);

const files = jsFiles();
if (!files.includes(ENTRY)) {
  console.error(`build: ${ENTRY} missing from dist/`);
  process.exit(1);
}

// Minify in place. No bundling, no target downlevelling: tsc already emitted
// for the configured target and a second opinion would undo that.
run("npx", [
  "esbuild",
  ...files.map((name) => join("dist", name)),
  "--minify",
  "--outdir=dist",
  "--allow-overwrite",
  "--log-level=warning",
]);

// No downlevel helpers (S4.1). tsc injects these when it compiles modern
// syntax for an older target; they are dead weight against the 15 KB budget
// and a sign the target and the syntax have drifted apart. Checked rather than
// assumed, because it changes silently when someone edits tsconfig.
const HELPERS = /__awaiter|__generator|__assign|__spread|__extends|__rest|tslib/;
for (const name of jsFiles()) {
  const text = readFileSync(join(DIST, name), "utf8");
  if (HELPERS.test(text)) {
    console.error(`FR-201: ${name} contains a tsc downlevel helper; raise the target or avoid the syntax.`);
    process.exit(1);
  }
}

let total = 0;
const report = [];
for (const name of jsFiles()) {
  const bytes = readFileSync(join(DIST, name));
  const gz = gzipSync(bytes, { level: 9 }).length;
  total += gz;
  report.push({ name, raw: bytes.length, gz });
}

const entryBytes = readFileSync(join(DIST, ENTRY));
const sri = `sha384-${createHash("sha384").update(entryBytes).digest("base64")}`;
const contentHash = createHash("sha256").update(entryBytes).digest("hex").slice(0, 16);

writeFileSync(
  join(DIST, "manifest.json"),
  JSON.stringify(
    {
      entry: ENTRY,
      integrity: sri,
      contentHash,
      gzippedTotalBytes: total,
      budgetBytes: BUDGET_BYTES,
      files: report,
    },
    null,
    2,
  ) + "\n",
);

for (const { name, raw, gz } of report) {
  console.log(`  ${name.padEnd(16)} ${String(raw).padStart(7)} raw  ${String(gz).padStart(6)} gz`);
}
console.log(`  ${"TOTAL".padEnd(16)} ${" ".repeat(12)}${String(total).padStart(6)} gz  (budget ${BUDGET_BYTES})`);
console.log(`  integrity: ${sri}`);

if (total > BUDGET_BYTES) {
  console.error(`\nFR-201: widget is ${total} bytes gzipped, over the ${BUDGET_BYTES} budget.`);
  process.exit(1);
}
console.log(`\nFR-201: ${total} / ${BUDGET_BYTES} bytes gzipped (${Math.round((total / BUDGET_BYTES) * 100)}% of budget).`);
