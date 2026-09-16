# Dzongkha Web Translation & Read-Aloud Middleware

Working name: **dzweb**. Owner: Digital Service Development Division, GovTech Agency, Royal Government of Bhutan.

## What this is

A service that any Bhutanese government website can call to translate its pages from English into Dzongkha, and to have those pages read aloud. It wraps existing GovTech NMT and TTS models; it does not train them.

Read `docs/01-srs.md` before implementing anything. Requirement IDs (FR-xxx, NFR-xxx) are the vocabulary of this repo — commit messages, PR bodies, and test names cite them.

## Non-negotiable invariants

These are correctness properties, not preferences. A change that breaks one of these is a defect regardless of what else it improves.

1. **Entities are never altered.** Numbers, dates, currency amounts, citizen IDs, file reference numbers, emails and URLs pass through translation byte-identical. Target is 100%, not "high". See FR-140.
2. **Fail open to English.** If translation, TTS, cache, or any upstream fails, the page renders in English. Never render a broken page, a spinner that never resolves, or a partial translation. See NFR-410.
3. **Never replace DOM text nodes in the browser widget.** Mutate `node.nodeValue` in place. Replacing nodes crashes React and Vue host pages. See FR-210.
4. **Tier 1 content is never live-translated.** The orchestrator rejects live MT for content marked `tier: 1`. This is a code path, not a policy document. See FR-510.
5. **Rendering artefacts never enter the cache or the TTS input.** Zero-width spaces inserted for Tibetan line-breaking are applied at render time only. See FR-160, FR-330.
6. **Cache keys include model and glossary version.** A terminology update must invalidate affected entries automatically. See FR-150.

## Domain facts you will not know

- Dzongkha uses Tibetan (Uchen) script. There are **no spaces between words**. The tsheg `་` (U+0F0B) separates syllables, not words. Browser line-breaking for this script is unreliable — expect overflow, not wrapping.
- A single syllable can stack up to four characters vertically. Default `line-height` clips them. Use ~2.0 and ~1.3× the Latin font size.
- Sentence terminators are shad `།` (U+0F0D) and double shad `༎` (U+0F0E), not full stops.
- **No browser speech engine and no screen reader ships a Dzongkha voice.** `window.speechSynthesis` is useless here. All audio comes from our own TTS service.
- The NMT model is NLLB-based and has never seen HTML. It will delete, duplicate or invent tags. Markup must be removed before the model and restored after.
- Upstream models are served via WSO2. Treat them as slow, rate-limited, and occasionally down.

## Stack

Python 3.11 / FastAPI for the orchestrator. PostgreSQL for translation memory and audit. Redis for hot cache. S3-compatible object storage for audio. TypeScript (no framework, no build step) for the browser widget — it must be under 15 KB gzipped and run on a five-year-old Android device.

## Testing

`pytest` for the orchestrator, `vitest` for the widget. Every fix gets a regression test. The entity-preservation and tag-integrity suites are gates: if either fails, nothing ships.

Fixtures live in `tests/fixtures/`. `tests/fixtures/gov-pages/` holds real government page snapshots — treat them as read-only golden data.

## Verification

<!-- gstack:verify: make check -->

`make check` runs lint, type check, both unit suites, and the two gate suites.

## gstack

Use the `/browse` skill for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review, /design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy, /canary, /benchmark, /browse, /connect-chrome, /qa, /qa-only, /design-review, /scrape, /setup-browser-cookies, /setup-deploy, /setup-gbrain, /retro, /investigate, /document-release, /document-generate, /codex, /cso, /autoplan, /plan-devex-review, /devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn, /spec

### Rules for this repo

- **`/guard` is on by default for work touching `orchestrator/pipeline/`.** That is where entity masking and tag restoration live; an accidental drive-by edit there is a citizen-facing defect.
- **Run `/cso` before every release.** This is public-sector infrastructure serving unauthenticated traffic and proxying third-party pages. SSRF, XSS through re-injected translations, and cache poisoning are the live threats.
- **`/qa` cannot judge Dzongkha rendering** unless the DDC Uchen font is installed in the browser it drives. Without the font, missing glyphs render as boxes and QA will report false failures — or worse, pass a broken layout. Check the font first; say so in the bug report if it is absent.
- **Never paste real citizen data into a session.** Test fixtures use synthetic IDs from `tests/fixtures/synthetic-entities.json`.
- **Telemetry stays off.** `gstack-config set telemetry off`. Verify with `gstack-egress grants` before starting work on a government machine.
- `/browse` drives a real browser with real logged-in sessions. Do not run it while signed into government admin consoles.
