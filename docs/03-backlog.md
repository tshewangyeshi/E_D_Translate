# Backlog
## Dzongkha Web Translation & Read-Aloud Middleware (dzweb)

Stories are sized for one gstack sprint each: `/spec` or `/autoplan` → implement → `/review` → `/qa` where applicable → `/ship`. Each cites the requirements it satisfies. A story is done when its acceptance criteria pass **and** a test bearing the requirement ID exists.

Sequence within an epic is dependency-ordered. Epics E1 and E2 are not parallelisable with anything — everything else depends on the pipeline existing. **E0 (Sprint 0) comes before everything:** it decides whether the placeholder approach works at all.

**Revision 2026-09-16:** updated with the remedies approved in `/plan-eng-review` (`docs/designs/dzweb-eng-review.md`), cited as `[ER-n]` / `[ER-On]`. Requirements introduced by the review carry proposed IDs from `docs/00-requirements.md` (status **P**, pending SRS-owner sign-off). **Pilot scope:** one citizen services portal through the widget. Audio (E5), proxy and CMS (E8) and the crawler (S9.2) are post-pilot.

---

## Definition of done

1. Acceptance criteria demonstrably met.
2. A test named for each requirement ID in the story (`test_fr140_...`).
3. `make check` green, including the entity and tag gate suites.
4. `/review` run, findings resolved or explicitly deferred with a reason in the PR body.
5. `/cso` run if the story touches the proxy, the API surface, or anything handling remote content.
6. Docs updated (`/document-release` runs automatically on `/ship`).
7. No new requirement introduced without an SRS amendment. If a story reveals a missing requirement, amend **`docs/00-requirements.md`** (the numbered SRS, S0.3) in the same PR. A CI check rejects tests or PR bodies citing an ID not defined there [ER-O8].

---

## E0 — Sprint 0: go/no-go

Nothing in E1 depends on a guess. This epic measures the real NMT endpoint and lands the numbered requirements before any pipeline code is written [ER-O1, ER-O7, ER-O8].

### S0.1 — Placeholder survival go/no-go
**As** the team, **I need** to know which placeholder format survives the real NLLB deployment, **so that** restoration isn't built on a token format that `<unk>`s or gets dropped.
Requirements: FR-120, FR-122, FR-140, FR-141 · measurement task [ER-O1]
- [ ] Ask the NMT operator whether custom tokens exist in the deployed tokenizer; record the answer
- [ ] ~20 public pilot-portal page snapshots committed to `tests/fixtures/gov-pages/` (personal data replaced with `synthetic-entities.json` values)
- [ ] Each masked block sent to WSO2 **once** per candidate format (at least 3, e.g. `⟦N⟧`, `<x1/>`-style, spelled sentinels); raw responses recorded to `tests/fixtures/mt-replay/`, keyed by source hash + model version + format
- [ ] Report: survival rate per format; block fallback rate by cause (entity lost / altered / duplicated / invented, tag lost / duplicated / reordered, text in non-existent run, glossary term missing, leak scan hit, upstream error) and by block type (heading, paragraph, paragraph-with-link, table cell, form label, list item)
- [ ] **Gate before S1.5:** masker recall 100% on a held-out set of pages; leak scan 0; block fallback ≤ agreed threshold (proposed 20%). Decision recorded in the repo
- [ ] S1.4 mock modes and rates are calibrated from these recordings
*gstack:* `/spec` → run → decision recorded; `/codex` second opinion if GovTech policy allows (see `TODOS.md`)

### S0.2 — WSO2 capacity measurement
Requirements: NFR-100, NFR-412, FR-156 · [ER-O7]
- [ ] Measured and documented: requests per second, maximum batch size, maximum input length, p95 latency, behaviour at the limit (429 vs queueing)
- [ ] Capacity note in the spec: pilot pages × segments per page vs quota, including the offline pre-warm time
- [ ] Token-bucket sizes for the quota manager (§2.10) derived from these numbers

### S0.3 — Numbered requirements in the repo
Requirements: all · [ER-O8]
- [x] A numbered requirements file is committed as `docs/00-requirements.md` (reconstructed draft, 2026-09-18); `01-srs.md` points to it
- [ ] Replaced or confirmed by the original SRS from DSDD
- [x] Every previously UNNUMBERED item in `02-technical-spec.md` and this backlog has a proposed ID (status **P**) in the reconstructed `docs/00-requirements.md`
- [ ] SRS owner signs off: confirms **R** rows, defines **?** rows, accepts or renumbers **P** rows
- [x] CI check (`tools/check_req_ids.py`, in `make check`): any `test_fr…`/`test_nfr…` name or `FR-`/`NFR-` citation in code and tests must exist in `docs/00-requirements.md` and must not be a **?** row
*Blocks sprint 1.*

---

## E1 — Pipeline foundations

The engine. Nothing else can start except E0. **Sprint 1 builds what the widget pilot uses:** the browser extractor and the server's segment grammar. The Python DOM extractor moves to E8 (S8.0b) [ER-O6].

### S1.1 — Widget block extraction and encoding
**As** the widget, **I need** to identify which text on a page may be translated and encode each block with its placeholders, **so that** code, scripts and opted-out content are never sent, and every translation can be written back into the same text nodes.
Requirements: FR-110, FR-111, FR-112, FR-113, FR-120, FR-121 · [ER-2, ER-O6, ER-18]
- [x] `adapters/widget/src/extract.ts` runs under jsdom in vitest
- [x] Text inside `script`, `style`, `code`, `pre`, `kbd`, `samp`, `var`, `textarea` is excluded
- [x] `translate="no"`, `class="notranslate"`, `data-no-translate`, `data-dz-skip` and configured `private_selectors` exclude the element and all descendants [ER-O3]
- [x] Elements with `lang` starting `dz` are excluded
- [x] `alt`, `title`, `placeholder` and submit/button `value` are extracted as attribute units; **`aria-label` and `aria-description` are not** [ER-18]
- [x] Whitespace-only and punctuation-only nodes produce no unit
- [x] A paragraph with inline links and emphasis yields one segment plus an ordered list of its text nodes (runs); void elements become `⟦vN/⟧` markers with no run; nested blocks: innermost wins, and outer text on each side becomes separate segments [ER-2]
- [x] Literal `⟦`/`⟧` in source are escaped
- [x] Extraction is order-stable: the same document yields the same unit sequence every time
- [x] **Shared golden fixtures** `tests/fixtures/extraction/*.json` (HTML → expected segments and runs) pass here and in pytest (S1.2, S8.0b)
*gstack:* `/spec` → implement → `/review`

### S1.2 — Server segment grammar
**As** the orchestrator, **I need** to parse and validate placeholder segments from any adapter, **so that** malformed or hostile input never reaches the model.
Requirements: FR-120, FR-121, FR-124 · [ER-2, ER-10]
- [x] Parses wire markers `⟦N⟧…⟦/N⟧`, `⟦vN/⟧`; rejects unescaped delimiters, unbalanced or unknown markers with a stable cause (mapping to `tag_fallback` lands with the API, S2.1); **client input may not contain entity tokens**
- [x] Round-trip without translation (identity model) reproduces the segment byte-for-byte (Hypothesis property test)
- [x] Segments over the model limit split at shad or terminal punctuation, never inside a placeholder
- [ ] Converts wire markers ↔ the model token format chosen in S0.1 — *two candidates implemented (`wire`, `xml`); final choice waits for S0.1*
- [x] Passes the shared extraction fixtures
*gstack:* `/spec` → implement → `/review`

### S1.3 — Entity masking
**As** a citizen, **I need** dates and amounts in a notice to be exactly right, **so that** I do not miss a deadline or misread an amount.
Requirements: FR-140, FR-141 · Gate: NFR-201 · [ER-9, ER-10, ER-12]
- [x] URLs, emails, 11-digit IDs, reference numbers, currency amounts (`Nu.`, `BTN`, `Ngultrum`), ISO/numeric/**word-month** dates, percentages and numbers are masked before the model (`orchestrator/pipeline/protect.py`)
- [x] **Catch-all numeric pattern:** no digit run of any length, in any script, reaches the model unmasked (`1500`, `2026`, `90000001` are regression cases)
- [x] Currency patterns match before bare numbers
- [x] **Exact multiset:** each entity token appears exactly once in output; duplicated, invented, truncated or missing tokens → `entity_check_failed` with source text
- [x] **Leak scan:** any numeral (ASCII, Tibetan, other scripts, fractions), email or URL outside restored entities and glossary terms → `entity_check_failed`
- [x] **No merged numbers** (found by the gate): entities newly touching across invisible inline tags → `entity_check_failed`
- [x] All masks restore exactly from **the current request's** entity map; output entities are byte-identical to input
- [x] Hypothesis property tests: entity-rich sentences mask and restore byte-identical; any digit run in any script is masked
- [x] Recall tool (`tools/masker_recall.py`, in `make check`): 100% on a synthetic labelled set with held-out split
- [ ] Masker recall on **hand-labelled pilot snapshots** with a held-out set — *waits for Sprint 0 snapshots (S0.1)*
- [x] The adversarial mock cannot produce an altered entity in output — 10,000 seeded runs across both candidate token formats (`tests/orchestrator/gates/test_entity_gate.py`); verified by mutation to catch the merged-number bug
*gstack:* `/spec` → implement → `/review` → `/codex` (second opinion if policy allows — this is the highest-consequence module)

### S1.4 — Adversarial model mock
**As** a developer, **I need** a model mock that misbehaves on purpose, **so that** restoration and validation are tested against realistic failure rather than a cooperative stub.
Requirements: supports FR-122, FR-123, FR-141
- [x] Mock modes: well-behaved, drops placeholders, duplicates placeholders, reorders placeholders, **invents placeholders, truncates tokens, converts digits to Tibetan, invents numbers**, mangles mask tokens, returns empty, times out, returns 503 (`orchestrator/testing/mock_nmt.py`)
- [ ] Mode mix and rates calibrated from S0.1 recordings — *waits for WSO2 access*
- [x] Deterministic under a seed
- [x] Used by default in unit tests; real endpoint only in integration tests
*Build this before S1.5.*

### S1.5 — Restoration and tag validation
Requirements: FR-122, FR-123 · Gate: NFR-200 · [ER-2, ER-10]
- [ ] Placeholder multiset **and order** in output are compared to input by the same shared validator as entities; mismatch records a tag-integrity failure
- [ ] **Widget responses:** on mismatch the segment returns `tag_fallback` with source text, and the widget keeps the block English (no DOM restructuring, FR-210)
- [ ] **Proxy/CMS/html responses:** formatting collapse produces valid markup with the segment's formatting applied to the whole
- [ ] Malformed markup is never emitted, under any mock mode
- [ ] Tag integrity rate is computed and exposed per batch
*Blocked by the S0.1 gate.* *gstack:* `/spec` → implement → `/review` → `/codex` (if policy allows)

### S1.6 — Glossary substitution
Requirements: FR-400, FR-401, FR-402 · [ER-7]
- [x] Longest-match-first, whole-word substitution from a versioned termbase (`orchestrator/pipeline/glossary.py`)
- [x] Case-sensitive matching per entry flag
- [x] The English term never reaches the model; the approved Dzongkha string always appears in output, or the block falls back (`glossary_term_missing`) — tested against the adversarial mock
- [x] Each term carries `term_id` + `term_version`; each segment computes its glossary fingerprint `gfp` from matched terms; bumping one term changes only segments using it
- [x] Glossary compliance rate computed per batch (`ComplianceStats`); reporting it in API metrics lands with S2.3
- [x] Termbase loads from a versioned file with validation (duplicate ids, ambiguous sources, numerals in sources, delimiters, zero-width characters in targets rejected); the version is exposed on the `Termbase` object — adding it to API responses lands with S2.1
- [ ] Real DCDD termbase — *the sample file has dummy targets only*

### S1.7 — Cache and translation memory
Requirements: FR-150, FR-151, FR-410, FR-411, FR-510 · [ER-1, ER-12, ER-13, ER-14, ER-21, ER-O3]
- [x] Keys are computed from the **masked** normalised segment, language pair, derived `pipeline_version`, `gfp`, and (machine keys only) model version (`orchestrator/store/keys.py`)
- [x] Redis values and TM rows contain **masked text only**; "Pay Nu. 500" and "Pay Nu. 600" share one entry and each restores its own amount; whitespace variants share a key and keep their own bytes
- [x] **Tier gate before lookup** (`orchestrator/store/lookup.py`): Tier 1 reads only approved translations; machine values in the approved namespace are ignored; unknown or non-integer tiers are Tier 1; `test_fr510_tier1_never_served_cached_mt`
- [x] Lookup order for Tier 2+: approved → machine → miss (live within budget / `pending_mt` land with S2.1 and S2.4); an approval supersedes a cached machine translation immediately (FR-421)
- [x] `pipeline_version` is derived (`orchestrator/pipeline/version.py`) from pattern data, grammar, split terminators and a golden corpus; tests prove a pattern change or a golden-output change alters it
- [x] Data model: `segment` / immutable `translation_version` (database trigger refuses content updates and deletes) / mutable `review_item`; approving creates a new version row; no tier column (`orchestrator/store/migrations/0001_initial.sql`)
- [x] Publishing a term change invalidates **exactly** the versions containing that term (index `glossary_hit(term_id, segment_key, gfp)`, robust to re-keying); approved → `needs_recheck`, not served; cache entries purged. *Rate-capped re-warm arrives with the S2.4 queue.*
- [x] A `pipeline_version` change migrates approved rows: re-key if unchanged, `needs_recheck` if changed
- [x] One batched TM query per request (asserted); cache backfill
- [x] Redis unavailable degrades to TM without error (`ResilientCache`; failures counted)
- [x] Tier 2 persistence only after N distinct clients (`should_persist`; counter failure = do not persist); `expire_machine` retention [ER-O3]
- [x] **PostgreSQL and Redis adapters verified against real services** (2026-09-18): the TM contract suite passes on PostgreSQL 16 as well as in memory, the database trigger refuses content updates and deletes, the Redis cache and distinct-client counter pass, migrations are idempotent; `python tools/check.py --require-integration` green

---

## E2 — API surface

### S2.1 — `/v1/translate`
Requirements: FR-100, NFR-100, NFR-412 · [ER-3, ER-O4, ER-O5, ER-O7, ER-O9, ER-21]
- [x] Batches up to 64 segments; 413 beyond (and for segments over 5,000 characters) (`orchestrator/api/app.py`)
- [x] Per-segment status, never all-or-nothing; each segment returns `segment_key` and, when translated, `origin` (`orchestrator/service/translate.py`)
- [x] Upstream failure returns 200 with source text and `upstream_error` (and is queued for retry); a storage outage is also 200 with source text, never a 5xx
- [x] Live MT only within the per-request budget (1.5 s) and only with quota-manager tokens (`orchestrator/upstream/quota.py`); the rest return `pending_mt` + enqueue
- [x] Every served translation is re-validated: entities byte-identical, glossary terms restored, tag markers exactly as in the source; invalid model output is never stored
- [x] p95 under 300 ms on a fully cached batch of 64, measured locally (test)
- [x] Under load beyond capacity, uncached work is **enqueued** (bounded depth); a full queue still answers with source text and is counted
- [x] HTTP caching: body-hash `ETag`; `Cache-Control: no-store` when any segment is `pending_mt`; pending responses are never 304'd; the ETag changes when the translation arrives
- [x] Keyless public route with enrolled-origin allowlist (403) and per-origin + per-client rate limits (429); JSON accepted as `text/plain` so browsers skip the CORS preflight
- [x] Tier resolution: strictest of site default, request hint and matched selector; a request cannot lower the tier (S3.1 adds server-side path rules)
- [x] Nothing is sent to MT or stored until N distinct clients have seen a Tier 2 segment (NFR-304)
- [ ] Real WSO2 translator client — *waits for WSO2 access; the mock implements the same interface*
- [ ] PostgreSQL job queue and worker — *S2.4; an in-memory queue implements the interface now*
- [ ] Rate limits shared across API replicas — *in-process for now; Redis-backed when more than one replica runs*

### S2.3 — Health, metrics, gateway publication
Requirements: FR-600, FR-610, FR-611 · [ER-O4]
- [ ] Health reports each upstream independently (NMT, PostgreSQL, Redis, queue depth, quota)
- [ ] Metrics: cache hit rate, tag integrity, entity preservation, glossary compliance, latency, upstream errors, fallback rate by cause, `pending_mt` rate, queue depth and age
- [ ] **Public widget routes** (`/translate`, `/config`, `/feedback`) published keyless (WSO2 passthrough if FR-600 requires); **server-to-server routes** use WSO2 per-consumer keys and quotas
- [ ] FR-600 wording confirmed with GovTech

### S2.4 — Job queue, worker and quota manager
Requirements: NFR-412, FR-155, FR-156, NFR-413 · [ER-3, ER-O7, ER-21]
- [ ] PostgreSQL job table, claimed with `FOR UPDATE SKIP LOCKED` in short transactions; partial index on pending jobs
- [ ] Unique on `machine_key` for **active** jobs only; invalidated keys can be enqueued again
- [ ] Visibility-timeout sweeper returns a crashed worker's jobs to pending (fault test)
- [ ] Enqueue skipped when a current translation already exists
- [ ] Token-bucket quota manager sized from S0.2 reserves ≥50% of upstream capacity for the worker; live attempts use leftover tokens only
- [ ] Offline pre-warm CLI translates every enrolled pilot page from snapshots through the worker before launch
- [ ] Rate-capped re-warm for glossary and model invalidations

---

## E3 — Governance

Deliberately early. Retrofitting tiering after adoption means renegotiating with every agency.

### S3.1 — Content tiering
Requirements: FR-500, FR-510, FR-511 · [ER-1, ER-6]
- [ ] **The server resolves the tier:** the strictest of the site default, site path rules, matched site selectors and the request's tier hint. A request can make content stricter, never looser
- [ ] Absent or unparseable tier, or an unknown site, resolves to Tier 1 (most restrictive)
- [ ] The tier gate runs **before any cache or TM lookup**; Tier 1 without an approved translation returns `tier_blocked` and the source text — verified by test, not by policy
- [ ] Test: a request claiming Tier 2 for a path or selector the site marks Tier 1 → `tier_blocked`
- [ ] `GET /v1/config` returns Tier 1 selectors and private selectors per enrolled site; path and selector rules are audited (S3.3)
- [ ] Review-item creation capped per site per day
- [ ] Tier 2 forces glossary and flags for review

### S3.2 — Machine-translation labelling
Requirements: FR-520, FR-521, FR-522 · [ER-O9]
- [ ] Persistent bilingual notice on any page carrying machine output
- [ ] Notice is not dismissable in a way that persists across pages
- [ ] `lang="dz-x-mtfrom-en"` on machine output, `lang="dz"` on approved output
- [ ] Notice carries a working report-an-error affordance, backed by S3.5
*gstack:* `/plan-design-review` before implementing — this is user-facing and easy to make ugly or ignorable.

### S3.3 — Audit trail
Requirements: FR-620
- [ ] Termbase changes, review approvals, offline seed imports, tier rule changes and site enrolment changes record actor, action, subject, timestamp
- [ ] Audit records are append-only

### S3.4 — Personal-data controls
Requirements: NFR-304, NFR-305, NFR-303 · [ER-O3]
- [ ] Pilot enrols public, unauthenticated pages only
- [ ] Widget loads nothing on pages marked `data-dz-private`; `data-dz-skip` and configured `private_selectors` regions are never extracted
- [ ] Server normalises numeric and ID-like path segments to `:id` before storage
- [ ] Tier 2 segments are neither persisted nor sent to MT until seen from ≥N distinct clients (proposed 3; salted, daily-rotated client hash); test: a one-off string never reaches the WSO2 mock
- [ ] Unapproved machine translations expire after the retention period (proposed 90 days)
- [ ] Logs contain `segment_key` hashes only, never segment text
*gstack:* `/cso` is a gate on this story.

### S3.5 — Minimal error-report intake
Requirements: FR-430, NFR-303 · [ER-O9, ER-18]
- [ ] `/v1/feedback` stores reports against `segment_key` (returned by `/v1/translate`)
- [ ] Rate limits: 10 reports/hour per client hash, 100/day per segment; honeypot field; limited or honeypot requests get 202 and are silently dropped (tested)
- [ ] No reporter identifier stored
- [ ] Triage and approval workflow stays in E7 (S7.2)

---

## E4 — Widget

### S4.1 — Core widget
Requirements: FR-200, FR-201, FR-210, FR-214, FR-215, NFR-502 · [ER-2, ER-6, ER-8, ER-16, ER-18, ER-O10]
- [ ] Single script tag, no host build step
- [ ] Widget build: `tsc` (no downlevel helpers, CI grep) → pinned minifier (no bundling) → content hash + SRI; no bundler, framework or polyfills
- [ ] Under 15 KB gzipped, measured on the final hashed file, enforced in CI
- [ ] Fetches `/v1/config` first; on failure offers no toggle and the page stays English
- [ ] Writes each translated run into its own existing text node; run count or order mismatch → block stays English; node identity (same `Text` objects, count, order) asserted in vitest
- [ ] Stale-response guard: a response is applied only if Dzongkha is still on, the block generation matches, and node values still equal the text that was sent
- [ ] `pending_mt` segments re-requested once after ~8 s
- [ ] Dzongkha-specific logic only in `locale-dz.ts`; CI grep (literal + escape forms) passes
- [ ] Auto-translate from a saved preference waits for `load` + idle + two quiet frames
- [ ] **Playwright fixtures: React CSR, React SSR (`hydrateRoot`), Vue CSR, Vue SSR** — translate, host re-render and toggle produce no framework errors or hydration warnings in the console
- [ ] API failure leaves the page in English with no uncaught exception
- [ ] **Device matrix:** Android System WebView / Chrome ~90, ~100 and current, with floors confirmed from pilot analytics; run before each release
*gstack:* `/spec` → implement → `/review` → `/qa` against the fixture host pages

### S4.2 — Dynamic content and attributes
Requirements: FR-113, FR-211 · [ER-11, ER-18, ER-20]
- [ ] Observer watches `childList` **and `characterData`**; content injected after load is translated
- [ ] A host change to translated text (value ≠ last written) updates the original and re-extracts the block; the widget's own writes are ignored by comparing against last-written values (no re-entrancy flag)
- [ ] Debounced dirty-set extraction of only affected blocks; an SPA route change produces one batch, not hundreds
- [ ] Initial pass in viewport order with main-thread yielding; off-screen blocks deferred via `IntersectionObserver`
- [ ] Performance test (Playwright, CPU throttled 6×, 3,000-node page with a 250 ms ticking counter): no widget long task > 50 ms; input latency p95 < 100 ms
- [ ] `alt`, `title`, `placeholder` translated on initial and subsequent passes and restored exactly; `aria-label`/`aria-description` never touched

### S4.3 — Toggle and persistence
Requirements: FR-212, FR-213 · [ER-11, ER-19]
- [ ] Toggle restores original text exactly, including whitespace
- [ ] **Regression:** host changes a fee from Nu. 500 to Nu. 600 while Dzongkha is on; toggle back shows Nu. 600
- [ ] Choice persists across pages on the same origin
- [ ] Repeated toggling does not accumulate state or leak memory: state lives in `WeakMap`s; vitest mounts/unmounts 1,000 blocks × 50 cycles and in-flight state returns to zero; Playwright heap snapshot shows no detached `Text` retained after 50 SPA route changes

### S4.4 — Host-page safety
Requirements: FR-210, NFR-300, NFR-401 · [ER-18]
- [ ] Translated text is inserted as text (`nodeValue` / `setAttribute`), never parsed as HTML
- [ ] A fixture page with a script-bearing translation response is not executed
- [ ] axe-core via Playwright on the four fixture hosts: **zero new violations** vs. the widget-absent baseline
*gstack:* `/cso` on this story specifically.

---

## E5 — Speech and player

**Post-pilot [ER-4].** Read-aloud is built after the translation pilot. FR-330 storage guards (no placeholders, mask tokens or zero-width characters in stored text) are enforced from E1/E6 onward, so this epic needs no rework of the pipeline. S5.2 still needs a DCDD reviewer, and spec open question 1 (TTS timing marks) must be answered first.

### S5.1 — TTS service integration
Requirements: FR-300, FR-301, FR-302, FR-330
- [ ] Content-addressed audio objects, immutable, long cache headers, range support
- [ ] Placeholders, mask tokens and zero-width characters raise before reaching TTS
- [ ] TTS unavailable degrades cleanly

### S5.2 — Text normalisation for speech
Requirements: FR-320, FR-321, FR-161
- [ ] Numbers, dates, currency, percentages, ordinals expanded per digit policy
- [ ] Abbreviation lexicon applied
- [ ] Latin-script runs handled per configured strategy
- [ ] Segment splitting at shad
*Needs a DCDD reviewer to confirm spoken forms. Do not guess these.*

### S5.3 — Player
Requirements: FR-310 to FR-316, FR-314, NFR-400
- [ ] Play/pause, ±10 s, previous/next segment, speed control
- [ ] First audio within 1 s for pre-generated content
- [ ] Current segment visually indicated
- [ ] No autoplay
- [ ] Every control keyboard operable with visible focus; targets ≥ 44 × 44 px
- [ ] Dzongkha text remains on screen throughout
- [ ] Audio model is a per-segment playlist, even where v1 has one element per segment
*gstack:* `/plan-design-review` → `/design-html` → implement → `/design-review` → `/qa`

### S5.4 — Section index
Requirements: FR-315
- [ ] A listener can reach any top-level section without traversing the page linearly
- [ ] Reachable by keyboard and by touch
*Test this with non-reading users before considering it done. Automated testing cannot tell you whether it works.*

---

## E6 — Rendering

### S6.1 — Dzongkha type and font
Requirements: FR-340, FR-341
- [ ] Self-hosted subsetted WOFF2 with `font-display: swap` and a documented fallback
- [ ] Type scale exposed as CSS custom properties
- [ ] Four-character stacked syllables render unclipped at default settings
- [ ] **Ships an OFL-licensed Tibetan fallback font (e.g. Noto Serif Tibetan, licence confirmed by GovTech) as the default** [ER-O9]
- [ ] DDC Uchen replaces the fallback once its web-embedding licence is confirmed (no longer a blocker for the pilot)
- [ ] Translated blocks size from the block's original font size (`--dz-base`), so nested translated blocks don't compound 1.3 × 1.3; handled-but-untranslated blocks reset to host typography

### S6.2 — Line-breaking
Requirements: FR-160, NFR-500 · [ER-8]
- [ ] Long Dzongkha strings wrap within their container rather than overflowing
- [ ] Break assistance is applied at render only; cache, TM and TTS input contain no inserted characters — verified by asserting on stored values
- [ ] Widget rule lives in `adapters/widget/locale-dz.ts`, server rule in `locale/dz.py`; a shared JSON fixture proves both insert breaks at the same positions

### S6.3 — Rendering conformance suite
Requirements: verifies FR-340, FR-341, FR-160
- [ ] A conformance page covering stacked syllables, long unbroken strings, mixed English–Dzongkha runs, form labels, table headers and buttons
- [ ] Run on Chrome, Firefox, Safari and a low-end Android browser before each release
- [ ] The suite fails loudly if the Uchen font is absent rather than reporting a layout failure
*gstack:* `/qa` — but read the font caveat in `CLAUDE.md` first.

---

## E7 — Review workflow

S7.0 is **pre-pilot**; the rest of E7 is post-pilot.

### S7.0 — Offline Tier 1 seed (pre-pilot)
**As** a citizen on the pilot portal, **I need** the fee and eligibility text in Dzongkha, **so that** the pilot proves the value on the pages that matter most, even before the reviewer UI exists.
Requirements: FR-410, FR-411, FR-413, FR-510, FR-620 · [ER-O2]
- [ ] Named DCDD reviewers and a review timeline are agreed (dependency for sprint 4)
- [ ] `ops/seed.py export` extracts every Tier 1 segment from the pilot snapshots to XLIFF/spreadsheet, with entity and tag placeholders **locked** (visible, not editable)
- [ ] Reviewers translate and approve offline
- [ ] `ops/seed.py import` validates each row's placeholder multiset and order and rejects invalid rows with a reason; valid rows become `translation_version` (`origin = human`) + `review_item = approved`, each with an audit event
- [ ] Before launch, a report lists Tier 1 coverage per pilot page (approved / still English)

### S7.1 — Reviewer interface
Requirements: FR-420, FR-421
- [ ] Segments pending review, filterable by site and tier
- [ ] Approval takes effect without redeployment
- [ ] An approved segment supersedes machine output everywhere it appears
*gstack:* `/plan-devex-review` — DCDD translators are the users and they are not developers.

- [ ] Approving creates a new immutable `translation_version`; `needs_recheck` items show the previous approved text and the glossary or masker change that triggered the recheck [ER-14, ER-7]
- [ ] Editor refuses to save if entity placeholders are missing, duplicated or altered, or tag placeholders are out of order

### S7.2 — Public error reporting (triage)
Requirements: FR-430, FR-431 · [ER-O9, ER-18]
- [ ] Builds on the S3.5 intake (reports already queue against `segment_key`, rate-limited, honeypot, no reporter identifier)
- [ ] Reviewers triage reports in the reviewer interface
- [ ] Accepted reports become approved TM entries

### S7.3 — Termbase management
Requirements: FR-403 · [ER-7]
- [ ] DCDD can add, amend, retire and publish entries
- [ ] Publication bumps `term_version` for changed terms and invalidates **only** segments containing them; machine rows re-warm rate-capped, approved rows → `needs_recheck`
- [ ] Changes are audited

---

## E8 — Proxy

Last, deliberately. It is the highest-risk component and the lowest-urgency one.

### S8.0 — Shared hardened fetcher
Requirements: NFR-301, NFR-302, FR-223 · [ER-5]
- [ ] `ops/fetch.py` is the **only** module that fetches remote page content (CI check); used by the proxy and the crawler (S9.2)
- [ ] Resolves the enrolled site record; refuses redirects to non-allowlisted hosts
- [ ] Refuses literal IPs and internal, loopback, link-local and metadata ranges; **checks the IP after DNS resolution and again on the connected socket** (DNS-rebinding fault test)
- [ ] GET/HEAD only; cookies and auth headers stripped both ways; response size and time caps
*gstack:* `/cso` is a gate on this story.

### S8.0b — Server-side DOM extraction
Requirements: FR-110, FR-111, FR-112, FR-113, FR-114 · [ER-O6]
- [ ] Python `extract.py` passes the shared extraction fixtures from S1.1
- [ ] Adds `<title>` and meta/OG description units (FR-114)

### S8.0c — `/v1/translate/html` (moved from E2)
Requirements: FR-101, FR-114 · [ER-5, ER-O6]
- [ ] Round-trips a real government page fixture with structure unchanged
- [ ] Translates title and meta description
- [ ] Returns per-document stats
- [ ] `url` is metadata only; the server never fetches it (test)
- [ ] Server-to-server route with WSO2 consumer keys

### S8.1 — Proxy core
Requirements: FR-220, FR-221
### S8.2 — Proxy security
Requirements: FR-222, FR-223, NFR-301, NFR-302
- [ ] All fetching goes through S8.0 (allowlist resolution, redirect and IP rules, GET/HEAD only, header stripping)
- [ ] Apparent authenticated sessions abort with an explanation
- [ ] Cache keys derive only from validated fields
*gstack:* `/cso` is a gate on this story, not an afterthought. Do not expose the proxy publicly until its findings are closed.
### S8.3 — Proxy SEO
Requirements: FR-224

---

## E9 — Operations

### S9.1 — Site enrolment
Requirements: FR-601
### S9.2 — Warm-cache crawler (post-pilot)
Requirements: FR-152, FR-303, NFR-102 · [ER-5, ER-O7]
- [ ] Fetches only through S8.0; `/cso` gates this story
- [ ] Scheduled crawl pre-translates (and, after E5, pre-synthesises) enrolled sites through the S2.4 queue
- [ ] Crawler load is bounded by the quota manager and cannot starve live traffic or the worker's reserved share
- [ ] Crawled content goes through the same S3.4 personal-data controls
*The pilot is pre-warmed offline from snapshots instead (S2.4).*
### S9.3 — Operations dashboard
Requirements: FR-612

---

## E10 — Evaluation harness

Runs alongside E1; the gates cannot be enforced without it.

### S10.1 — Frozen evaluation set
Requirements: NFR-202
- [ ] 500–1,000 segments across all three tiers, sourced from real government content
- [ ] Held in a separate repository or a protected path; never used for tuning
- [ ] chrF++ scored on every model or glossary change
### S10.2 — Release gates in CI
Requirements: NFR-200, NFR-201 · [ER-15]
The adversarial mock breaks tags at a configured rate, so a *rate* measured against it describes the mock, not dzweb. Gates are split by what each can prove:
- [ ] **Every build (mock):** across all mock modes and 10,000 seeded runs — zero malformed markup emitted, zero altered or duplicated entities, zero partial restores. Tests named `test_nfr200_*` / `test_nfr201_*`
- [ ] **Every build (replay):** the recorded real responses from S0.1 pass with tag integrity ≥ 99% and entity preservation 100%
- [ ] **Nightly (real endpoint):** on the frozen evaluation set (S10.1), tag integrity below 99% or entity preservation below 100% fails; the trend is reported

### S10.4 — Fault-injection suite
Requirements: NFR-410, NFR-412, FR-510, NFR-301 · [ER-17]
- [ ] `tests/fault/` runs in `make check` against real PostgreSQL and Redis (containers) and a controllable WSO2 mock
- [ ] Cases: Redis down; PostgreSQL down (tier gate fails closed to source text, still 200); WSO2 slow / 503 / garbage; worker killed mid-job → reclaimed after visibility timeout; invalidated key re-enqueues; stampede → live attempts shed to a bounded queue; quota exhausted → live skipped, worker proceeds; fetcher refuses a redirect to 169.254.169.254 and a DNS answer that changes to 10.x between resolve and connect
- [ ] Each case asserts HTTP 200, source text where applicable, and an emitted metric
- [ ] Redis-down case: p95 < 800 ms on a 64-segment cached batch (batched TM read + LRU) [ER-21]
### S10.3 — Performance benchmarks
Requirements: NFR-100, NFR-101, NFR-103, NFR-104
*gstack:* `/benchmark` before and after each release.

---

## Suggested sprint order

*Re-sequenced by `/plan-eng-review` 2026-09-16 [ER-4, ER-O1, ER-O2, ER-O6, ER-O7, ER-O9].*

| Sprint | Stories | Milestone |
|---|---|---|
| 0 | S0.1, S0.2, S0.3 | **Go/no-go:** placeholder format proven on the real endpoint, capacity measured, numbered SRS in repo |
| 1 | S1.1, S1.2, S1.4 | Widget extraction and server segment grammar round-trip cleanly (shared fixtures) |
| 2 | S1.3, S1.5, S10.2 | The two gates exist and pass (mock safety + replayed real responses) |
| 3 | S1.6, S1.7, S2.1, S2.4 | API serving masked, tier-gated, glossary-enforced translations; queue and quota working |
| 4 | S3.1, S3.2, S3.3, S3.4, S3.5, S2.3 | Governance and personal-data controls enforced before first external use; S7.0 offline review starts |
| 5 | S4.1, S4.2, S4.4, S10.4 | Widget translating a real pilot page; fault suite green |
| 6 | S6.1, S6.2, S6.3 | It looks right on real devices (OFL fallback font) |
| 7 | S4.3, S7.0 import, S10.1, offline pre-warm (S2.4) | Tier 1 seeded; pilot pages pre-warmed |
| 8 | Pilot hardening, `/cso`, device matrix | **Pilot-ready; usability testing begins** |
| 9+ | E5, E7 (S7.1–S7.3), E9, E8 | Audio, review UI, operations, then proxy |

Phase 0 of the roadmap in the presentation now ends at sprint 4, after Sprint 0. Phase 1 ends at sprint 8.
