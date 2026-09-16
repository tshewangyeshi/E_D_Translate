# Backlog
## Dzongkha Web Translation & Read-Aloud Middleware (dzweb)

Stories are sized for one gstack sprint each: `/spec` or `/autoplan` → implement → `/review` → `/qa` where applicable → `/ship`. Each cites the requirements it satisfies. A story is done when its acceptance criteria pass **and** a test bearing the requirement ID exists.

Sequence within an epic is dependency-ordered. Epics E1 and E2 are not parallelisable with anything — everything else depends on the pipeline existing.

---

## Definition of done

1. Acceptance criteria demonstrably met.
2. A test named for each requirement ID in the story (`test_fr140_...`).
3. `make check` green, including the entity and tag gate suites.
4. `/review` run, findings resolved or explicitly deferred with a reason in the PR body.
5. `/cso` run if the story touches the proxy, the API surface, or anything handling remote content.
6. Docs updated (`/document-release` runs automatically on `/ship`).
7. No new requirement introduced without an SRS amendment. If a story reveals a missing requirement, amend `01-srs.md` in the same PR.

---

## E1 — Pipeline foundations

The engine. Nothing else can start.

### S1.1 — Node extraction
**As** the orchestrator, **I need** to identify which text on a page may be translated, **so that** code, scripts and opted-out content are never sent to the model.
Requirements: FR-110, FR-111, FR-112, FR-113
- [ ] Text inside `script`, `style`, `code`, `pre`, `kbd`, `samp`, `var`, `textarea` is excluded
- [ ] `translate="no"`, `class="notranslate"`, `data-no-translate` exclude the element and all descendants
- [ ] Elements with `lang` starting `dz` are excluded
- [ ] `alt`, `title`, `placeholder`, `aria-label`, and submit/button `value` are extracted as attribute units
- [ ] Whitespace-only and punctuation-only nodes produce no unit
- [ ] Extraction is order-stable: the same document yields the same unit sequence every time
*gstack:* `/spec` → implement → `/review`

### S1.2 — Block grouping and placeholders
**As** the orchestrator, **I need** to translate whole blocks rather than fragments, **so that** word-order differences between English and Dzongkha do not shred sentences.
Requirements: FR-120, FR-121, FR-124
- [ ] A paragraph containing inline links and emphasis yields exactly one segment
- [ ] Inline markup is represented by numbered placeholder pairs with a side map preserving tag and attributes
- [ ] Nested inlines collapse to the outermost pair under default strictness
- [ ] Round-trip without translation (identity model) reproduces the original markup byte-for-byte
- [ ] Segments over the model limit split at shad or terminal punctuation, never inside a placeholder
*gstack:* `/spec` → implement → `/review`

### S1.3 — Entity masking
**As** a citizen, **I need** dates and amounts in a notice to be exactly right, **so that** I do not miss a deadline or misread an amount.
Requirements: FR-140, FR-141 · Gate: NFR-201
- [ ] URLs, emails, 11-digit IDs, reference numbers, Ngultrum amounts, dates, percentages and numbers are masked before the model
- [ ] Currency patterns match before bare numbers
- [ ] All masks restore exactly; output entities are byte-identical to input
- [ ] A model response missing any mask token causes the segment to fall back to source text with `entity_check_failed`
- [ ] The adversarial mock (a model that randomly drops, duplicates and mangles tokens) cannot produce an altered entity in output — 10,000 randomised runs
*gstack:* `/spec` → implement → `/review` → `/codex` (second opinion — this is the highest-consequence module)

### S1.4 — Adversarial model mock
**As** a developer, **I need** a model mock that misbehaves on purpose, **so that** restoration and validation are tested against realistic failure rather than a cooperative stub.
Requirements: supports FR-122, FR-123, FR-141
- [ ] Mock modes: well-behaved, drops placeholders, duplicates placeholders, reorders placeholders, mangles mask tokens, returns empty, times out, returns 503
- [ ] Deterministic under a seed
- [ ] Used by default in unit tests; real endpoint only in integration tests
*Build this before S1.5.*

### S1.5 — Restoration and tag validation
Requirements: FR-122, FR-123 · Gate: NFR-200
- [ ] Placeholder multiset in output is compared to input; mismatch records a tag-integrity failure
- [ ] On mismatch, formatting collapse produces valid markup with the segment's formatting applied to the whole
- [ ] Malformed markup is never emitted, under any mock mode
- [ ] Tag integrity rate is computed and exposed per batch
*gstack:* `/spec` → implement → `/review` → `/codex`

### S1.6 — Glossary substitution
Requirements: FR-400, FR-401, FR-402
- [ ] Longest-match-first substitution from a versioned termbase
- [ ] Case-sensitive matching per entry flag
- [ ] The English term never reaches the model; the approved Dzongkha string always appears in output
- [ ] Glossary compliance rate reported per batch
- [ ] Termbase loads from a versioned file; version is exposed in API responses

### S1.7 — Cache and translation memory
Requirements: FR-150, FR-151, FR-410, FR-411
- [ ] Cache key includes normalised text, language pair, model version, glossary version, segmentation version
- [ ] Lookup order is approved → cached → live
- [ ] Publishing a new termbase version causes affected entries to miss
- [ ] Every translated segment persists with source, MT output, versions and tag integrity flag
- [ ] Redis unavailable degrades to TM/live without error

---

## E2 — API surface

### S2.1 — `/v1/translate`
Requirements: FR-100, NFR-100, NFR-412
- [ ] Batches up to 64 segments; 413 beyond
- [ ] Per-segment status, never all-or-nothing
- [ ] Upstream failure returns 200 with source text and `upstream_error`
- [ ] p95 under 300 ms on a fully cached batch of 64, measured locally
- [ ] Under load beyond capacity, uncached requests are shed rather than queued

### S2.2 — `/v1/translate/html`
Requirements: FR-101, FR-114
- [ ] Round-trips a real government page fixture with structure unchanged
- [ ] Translates title and meta description
- [ ] Returns per-document stats

### S2.3 — Health, metrics, gateway publication
Requirements: FR-600, FR-610, FR-611
- [ ] Health reports each upstream independently
- [ ] Metrics: cache hit rate, tag integrity, entity preservation, glossary compliance, latency, upstream errors
- [ ] Published through WSO2 with per-consumer keys and quotas

---

## E3 — Governance

Deliberately early. Retrofitting tiering after adoption means renegotiating with every agency.

### S3.1 — Content tiering
Requirements: FR-500, FR-510, FR-511
- [ ] Tier travels with the request and with the enrolled site's defaults
- [ ] Absent or unparseable tier defaults to tier 1 (most restrictive)
- [ ] Tier 1 live MT returns `tier_blocked` and the source text — verified by test, not by policy
- [ ] Tier 2 forces glossary and flags for review

### S3.2 — Machine-translation labelling
Requirements: FR-520, FR-521, FR-522
- [ ] Persistent bilingual notice on any page carrying machine output
- [ ] Notice is not dismissable in a way that persists across pages
- [ ] `lang="dz-x-mtfrom-en"` on machine output, `lang="dz"` on approved output
- [ ] Notice carries a working report-an-error affordance
*gstack:* `/plan-design-review` before implementing — this is user-facing and easy to make ugly or ignorable.

### S3.3 — Audit trail
Requirements: FR-620
- [ ] Termbase changes, review approvals and tier changes record actor, action, subject, timestamp
- [ ] Audit records are append-only

---

## E4 — Widget

### S4.1 — Core widget
Requirements: FR-200, FR-201, FR-210, FR-214, FR-215, NFR-502
- [ ] Single script tag, no host build step
- [ ] Under 15 KB gzipped, enforced in CI
- [ ] Text nodes mutated in place — a React fixture app re-renders after translation without error
- [ ] API failure leaves the page in English with no uncaught exception
- [ ] Works in Android WebView versions in common use in Bhutan
*gstack:* `/spec` → implement → `/review` → `/qa` against a fixture host page

### S4.2 — Dynamic content and attributes
Requirements: FR-113, FR-211
- [ ] Content injected after load is translated
- [ ] The observer does not re-trigger on the widget's own mutations
- [ ] Debounced; an SPA route change produces one batch, not hundreds
- [ ] Attributes translated on both initial and subsequent passes

### S4.3 — Toggle and persistence
Requirements: FR-212, FR-213
- [ ] Toggle restores original text exactly, including whitespace
- [ ] Choice persists across pages on the same origin
- [ ] Repeated toggling does not accumulate state or leak memory

### S4.4 — Host-page safety
Requirements: FR-210, NFR-300, NFR-401
- [ ] Translated text is inserted as text, never parsed as HTML
- [ ] A fixture page with a script-bearing translation response is not executed
- [ ] Automated accessibility score of the host page is unchanged with the widget present
*gstack:* `/cso` on this story specifically.

---

## E5 — Speech and player

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
- [ ] **Blocked until the DDC Uchen web-embedding licence is confirmed**

### S6.2 — Line-breaking
Requirements: FR-160
- [ ] Long Dzongkha strings wrap within their container rather than overflowing
- [ ] Break assistance is applied at render only; cache, TM and TTS input contain no inserted characters — verified by asserting on stored values

### S6.3 — Rendering conformance suite
Requirements: verifies FR-340, FR-341, FR-160
- [ ] A conformance page covering stacked syllables, long unbroken strings, mixed English–Dzongkha runs, form labels, table headers and buttons
- [ ] Run on Chrome, Firefox, Safari and a low-end Android browser before each release
- [ ] The suite fails loudly if the Uchen font is absent rather than reporting a layout failure
*gstack:* `/qa` — but read the font caveat in `CLAUDE.md` first.

---

## E7 — Review workflow

### S7.1 — Reviewer interface
Requirements: FR-420, FR-421
- [ ] Segments pending review, filterable by site and tier
- [ ] Approval takes effect without redeployment
- [ ] An approved segment supersedes machine output everywhere it appears
*gstack:* `/plan-devex-review` — DCDD translators are the users and they are not developers.

### S7.2 — Public error reporting
Requirements: FR-430, FR-431
- [ ] Reports queue against a segment key
- [ ] No reporter identifier is stored (NFR-303)
- [ ] Accepted reports become approved TM entries
- [ ] Rate limited and spam resistant

### S7.3 — Termbase management
Requirements: FR-403
- [ ] DCDD can add, amend, retire and publish entries
- [ ] Publication produces a new version and invalidates affected cache entries
- [ ] Changes are audited

---

## E8 — Proxy

Last, deliberately. It is the highest-risk component and the lowest-urgency one.

### S8.1 — Proxy core
Requirements: FR-220, FR-221
### S8.2 — Proxy security
Requirements: FR-222, FR-223, NFR-301, NFR-302
- [ ] Allowlist resolves to enrolled site records, not pattern matching on request input
- [ ] Redirects to non-allowlisted hosts refused; internal ranges and literal IPs refused
- [ ] Only GET and HEAD; cookies and auth headers stripped both ways
- [ ] Apparent authenticated sessions abort with an explanation
- [ ] Cache keys derive only from validated fields
*gstack:* `/cso` is a gate on this story, not an afterthought. Do not expose the proxy publicly until its findings are closed.
### S8.3 — Proxy SEO
Requirements: FR-224

---

## E9 — Operations

### S9.1 — Site enrolment
Requirements: FR-601
### S9.2 — Warm-cache crawler
Requirements: FR-152, FR-303, NFR-102
- [ ] Scheduled crawl pre-translates and pre-synthesises enrolled sites
- [ ] Crawler load is bounded and cannot starve live traffic
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
Requirements: NFR-200, NFR-201
- [ ] Tag integrity below 99% fails the build
- [ ] Entity preservation below 100% fails the build
- [ ] Both run against the adversarial mock and against the real endpoint nightly
### S10.3 — Performance benchmarks
Requirements: NFR-100, NFR-101, NFR-103, NFR-104
*gstack:* `/benchmark` before and after each release.

---

## Suggested sprint order

| Sprint | Stories | Milestone |
|---|---|---|
| 1 | S1.1, S1.2, S1.4 | Extraction and segmentation round-trip cleanly |
| 2 | S1.3, S1.5, S10.2 | The two gates exist and pass |
| 3 | S1.6, S1.7, S2.1 | API serving cached, glossary-enforced translations |
| 4 | S3.1, S3.2, S3.3, S2.3 | Governance enforced before first external use |
| 5 | S4.1, S4.2, S4.4 | Widget translating a real pilot page |
| 6 | S6.1, S6.2, S6.3 | It looks right on real devices |
| 7 | S5.1, S5.2, S5.3 | Audio works end to end |
| 8 | S4.3, S5.4, S9.2, S10.1 | Pilot-ready; usability testing begins |
| 9+ | E7, E9, E8 | Review workflow, operations, then proxy |

Phase 0 of the roadmap in the presentation ends at sprint 4. Phase 1 ends at sprint 8.
