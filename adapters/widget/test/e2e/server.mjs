// Fixture host pages for the browser tests (S4.1, ER-16).
//
// Four hosts the widget must survive: React and Vue, each rendered in the
// browser (CSR) and rendered on the server then hydrated (SSR). SSR is the one
// that matters most -- hydration compares the server's HTML against what the
// client renders, and a widget that rewrote the text in between is exactly the
// mismatch React and Vue complain about. jsdom cannot show that; only a real
// browser running real React can.
//
// Everything is served locally from node_modules. No CDN, so the tests run
// offline and cannot silently test a different framework version than the one
// pinned in package.json.

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, join, extname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WIDGET = dirname(dirname(HERE));
const MODULES = join(WIDGET, "node_modules");

import { build } from "esbuild";

import { CONTENT } from "./content.mjs";

export { CONTENT };

/**
 * Bundle a fixture entry for the browser.
 *
 * React 19 ships no UMD build, so the frameworks cannot be loaded as bare
 * browser modules. This bundles the FIXTURE only; the widget under test is
 * still served exactly as `scripts/build.mjs` produced it, unbundled, which is
 * the whole point of the exercise.
 */
const bundles = new Map();
async function serveBundle(res, name) {
  if (!bundles.has(name)) {
    const result = await build({
      entryPoints: [join(HERE, `entry-${name}.mjs`)],
      bundle: true,
      format: "esm",
      write: false,
      define: { "process.env.NODE_ENV": '"production"' },
      logLevel: "warning",
    });
    bundles.set(name, result.outputFiles[0].text);
  }
  res.writeHead(200, { "Content-Type": "text/javascript" });
  res.end(bundles.get(name));
}

/** What the stub orchestrator answers. "DZ:" stands in for Dzongkha. */
function translateReply(body) {
  return {
    model_version: "stub-1",
    glossary_version: "stub",
    segments: body.segments.map((s) => {
      // The legal line is Tier 1: refused, never machine translated (FR-510).
      if (s.text.includes("non-refundable")) {
        return { id: s.id, text: s.text, status: "tier_blocked" };
      }
      return { id: s.id, text: `DZ:${s.text}`, status: "translated", origin: "mt" };
    }),
  };
}

const TYPES = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html" };

async function serveFile(res, path) {
  try {
    const body = await readFile(path);
    res.writeHead(200, { "Content-Type": TYPES[extname(path)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}

function page(framework, mode, ssrHtml) {
  // The widget is loaded exactly as a host site would load it: one module
  // script tag with a data-dz-site attribute and nothing else.
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${framework} ${mode}</title></head>
<body>
<div id="app">${ssrHtml}</div>
<script type="module" src="/app/${framework}-${mode}.js"></script>
<script type="module" src="/widget/main.js" data-dz-site="portal" data-dz-api=""></script>
</body></html>`;
}

/** Server-render the fixture so hydration has something to compare against. */
async function ssrHtml(framework) {
  if (framework === "react") {
    const { renderToString } = await import("react-dom/server");
    const { createElement: h } = await import("react");
    const { tree } = await import("./app-react.mjs");
    return renderToString(tree(h, CONTENT));
  }
  const { renderToString } = await import("vue/server-renderer");
  const { createSSRApp, h } = await import("vue");
  const { options } = await import("./app-vue.mjs");
  return renderToString(createSSRApp(options(h, CONTENT)));
}

export async function start(port = 0) {
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const path = url.pathname;

    if (path === "/v1/config") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          site: "portal",
          default_tier: 2,
          tier1_selectors: [],
          private_selectors: [],
        }),
      );
      return;
    }
    if (path === "/v1/translate" && req.method === "POST") {
      let raw = "";
      for await (const chunk of req) raw += chunk;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(translateReply(JSON.parse(raw))));
      return;
    }
    if (path.startsWith("/widget/")) {
      return serveFile(res, join(WIDGET, "dist", path.slice("/widget/".length)));
    }
    if (path.startsWith("/app/")) {
      const name = path.slice("/app/".length).replace(/.js$/, "");
      return serveBundle(res, name);
    }

    if (path === "/perf") {
      // A long, framework-free page: 3,000 nodes and a counter ticking every
      // 250 ms, so the main thread is never idle. No framework, so whatever the
      // measurements show is the widget's cost and not React's.
      const blocks = Array.from(
        { length: 600 },
        (_, n) =>
          `<p>Section ${n}: apply to renew your passport online, ` +
          `fee <b>Nu. ${1000 + n}</b>, processed within 14 days.</p>`,
      ).join("");
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>perf</title></head>
<body>
<div id="app">${blocks}</div>
<script>
  // Keeps the page busy the way a real one is, so the widget is never
  // measured on an otherwise empty main thread.
  let tick = 0;
  const counter = document.createElement("span");
  counter.id = "tick";
  document.body.appendChild(counter);
  setInterval(() => { counter.textContent = "tick " + (++tick); }, 250);
</script>
<script type="module" src="/widget/main.js" data-dz-site="portal" data-dz-api=""></script>
</body></html>`);
      return;
    }

    const match = /^\/(react|vue)-(csr|ssr)$/.exec(path);
    if (match) {
      const [, framework, mode] = match;
      const html = mode === "ssr" ? await ssrHtml(framework) : "";
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(page(framework, mode, html));
      return;
    }
    res.writeHead(404).end("not found");
  });

  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  return { server, port: server.address().port };
}
