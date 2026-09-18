# Requirements — dzweb

**Status: RECONSTRUCTED DRAFT — needs sign-off by the SRS owner (DSDD, GovTech).**

The original numbered SRS is not in this repository. This file was reconstructed on 2026-09-18 (backlog S0.3) from how `02-technical-spec.md`, `03-backlog.md` and `01-srs.md` cite each ID. Where the original SRS exists, **it wins**: replace each row with the owner's wording and delete this banner.

Every row has a status:

| Status | Meaning |
|---|---|
| **R** | Reconstructed: wording inferred from the spec/backlog passages that cite the ID. Intent is well-evidenced; exact wording is not original. |
| **?** | Range-only: the ID exists (the docs cite a range such as `FR-500..511`) but no passage says what it means. **Definition required from the SRS owner.** Do not cite it in tests until defined. |
| **P** | Proposed: a new requirement introduced by `/plan-eng-review` (2026-09-16), previously marked UNNUMBERED. Numbered here in unused slots of the right family. Needs owner acceptance. `[ER-…]` points to `docs/designs/dzweb-eng-review.md`. |

`tools/check_req_ids.py` (run by `make check`) fails if a test name (`test_fr140_…`, `test_nfr410_…`) or a requirement citation in code, tests or PR text refers to an ID not listed here, or to a **?** ID.

---

## FR-1xx — Translation API and pipeline

| ID | Requirement | Status |
|---|---|---|
| FR-100 | `POST /v1/translate` translates a batch of up to 64 segments per request, with per-segment status; never all-or-nothing. | R |
| FR-101 | `POST /v1/translate/html` translates a whole HTML document and returns it with per-document stats. | R |
| FR-102 | HTTP caching reflects time-varying state: `ETag` is a hash of the response body; any response containing a non-final segment (`pending_mt`) is `Cache-Control: no-store`; all-final responses are `private`, short-lived and revalidated. [ER-O5] | P |
| FR-103 | Public widget routes (`/v1/translate`, `/v1/config`, `/v1/feedback`) are keyless and protected by the enrolled-origin allowlist plus per-origin and per-IP rate limits; server-to-server routes use gateway consumer keys and quotas. [ER-O4] | P |
| FR-104 | Each `/v1/translate` segment result carries a `segment_key` (hash of the masked source) that `/v1/feedback` accepts. [ER-O9] | P |
| FR-110 | Text inside `script`, `style`, `noscript`, `textarea`, `code`, `pre`, `kbd`, `samp`, `var` is never extracted. | R |
| FR-111 | Content opted out with `translate="no"`, `class="notranslate"`/`skiptranslate` or `data-no-translate` is excluded with all descendants. | R |
| FR-112 | Elements whose `lang` starts with `dz`, and whitespace-only or punctuation-only nodes, produce no unit; extraction is order-stable. | R |
| FR-113 | `alt`, `title`, `placeholder` and submit/button `value` are extracted and translated as attribute units. `aria-label` and `aria-description` are **not** translated while no screen reader ships a Dzongkha voice. [ER-18 amends] | R |
| FR-114 | In proxy and CMS modes, `<title>` and the meta/OG description are translated. | R |
| FR-120 | The translation unit is the nearest block-level element, not the individual text node. | R |
| FR-121 | Inline markup inside a block is represented by numbered placeholder pairs with a side map preserving tag and attributes; nested inlines collapse to the outermost pair by default. | R |
| FR-122 | The placeholder multiset and order in model output are validated against the input; a mismatch is a tag-integrity failure. In the widget, a failing block stays English. [ER-2 amends] | R |
| FR-123 | In proxy, CMS and `/translate/html` output, a tag-integrity failure falls back to formatting collapse: valid markup with the segment's formatting applied to the whole. | R |
| FR-124 | Segments longer than the model's maximum input split at shad, double shad or English terminal punctuation, never inside a placeholder. | R |
| FR-130 | Speech segments split at shad for segment-level synthesis. | R |
| FR-140 | Numbers, dates, currency amounts, citizen IDs, file reference numbers, emails and URLs are masked before the model and restored byte-identical. Target 100%. No digit run reaches the model unmasked. [ER-9] | R |
| FR-141 | If any entity token is missing, duplicated, unknown or malformed in model output, or any restored entity is not byte-identical, the segment returns its source text with `entity_check_failed`. Never partially restored. [ER-10] | R |
| FR-142 | After restoration, a leak scan rejects any ASCII or Tibetan digit (U+0F20–U+0F29), email or URL outside restored entities and glossary terms. [ER-9] | P |
| FR-143 | Cache and translation memory store masked text only; real entity values are never persisted; every response is restored from that request's own entity map. [ER-12] | P |
| FR-150 | Cache and TM keys include model version (machine translations) and glossary state, so model and terminology changes invalidate affected entries automatically. | R |
| FR-151 | A Redis hot cache sits in front of translation memory; Redis unavailability degrades to TM/live without error. | R |
| FR-152 | A warm-cache crawler pre-translates enrolled sites (post-pilot). | R |
| FR-153 | Glossary state in keys is a per-segment fingerprint of matched `(term_id, term_version)`; a term change invalidates only segments containing that term; machine rows re-warm rate-capped; approved rows using it move to `needs_recheck` and are not served until re-approved. [ER-7] | P |
| FR-154 | The pipeline version in keys is derived from masking pattern data, segmentation rules and the golden fixtures' masked output, never bumped by hand; a change migrates approved translations (re-key if unchanged, `needs_recheck` if changed). [ER-13] | P |
| FR-155 | Uncached Tier 2+ segments are translated live only within a per-request budget; the rest return `pending_mt` and are enqueued on a durable queue with crash recovery; shed load is enqueued, not dropped. [ER-3] | P |
| FR-156 | A quota manager sized to measured upstream limits reserves a share of NMT capacity for the background worker; live attempts use only leftover capacity. [ER-O7] | P |
| FR-160 | Zero-width spaces for Tibetan line-breaking are inserted at render time only; they never enter cache, TM or TTS input. | R |
| FR-161 | Numbers are rendered and spoken according to a configured digit policy. | R |

## FR-2xx — Adapters

| ID | Requirement | Status |
|---|---|---|
| FR-200 | The widget installs with a single script tag and needs no host build step. | R |
| FR-201 | The widget is under 15 KB gzipped, enforced in CI. | R |
| FR-202 … FR-209 | *(cited only via range `FR-200..215`)* | ? |
| FR-210 | The widget mutates existing text nodes in place (`nodeValue`); it never creates, removes, replaces or moves nodes in host-owned content. | R |
| FR-211 | Content added or changed after load is translated, debounced, without re-triggering on the widget's own writes. | R |
| FR-212 | The toggle restores the original text exactly, respecting any host change made while Dzongkha was shown. [ER-11 amends] | R |
| FR-213 | The language choice persists across pages on the same origin. | R |
| FR-214 | Translated blocks carry a `lang` attribute (`dz` for approved, `dz-x-mtfrom-en` for machine output). | R |
| FR-215 | On any network or API failure the widget leaves the page in English with no uncaught exception. | R |
| FR-216 | The widget loads site configuration (`GET /v1/config`) before offering translation; if configuration is unavailable it offers no toggle. [ER-6] | P |
| FR-217 | The widget discards any response that arrives after a toggle, a host text change or a newer extraction of the same block (stale-response guard). [ER-11] | P |
| FR-218 | Automatic translation from a saved preference starts only after load, idle and a quiet period, so server-rendered hosts finish hydration first. [ER-16] | P |
| FR-220 | Proxy core: serves an enrolled site's pages translated. | R |
| FR-221 | Proxy core: rewrites links to stay within the proxy. | R |
| FR-222 | Proxy security controls (see NFR-301, NFR-302). | R |
| FR-223 | The proxy and crawler fetch only with GET/HEAD, refuse redirects off the allowlist, and refuse literal and internal IPs. | R |
| FR-224 | The proxy injects `hreflang` pairs for search engines. | R |
| FR-225 | All server-side fetching of remote content goes through one hardened fetcher that checks IPs after DNS resolution and again on connect, caps size and time, and strips credentials. [ER-5] | P |
| FR-230 | CMS plugins (Drupal, WordPress) translate on save into a draft Dzongkha revision. | R |
| FR-231 | CMS translations publish only through a human review gate. | R |

## FR-3xx — Speech and rendering

| ID | Requirement | Status |
|---|---|---|
| FR-300 | Synthesise Dzongkha speech per segment via the TTS service (post-pilot). | R |
| FR-301 | Audio objects are content-addressed and immutable. | R |
| FR-302 | Audio is served with long cache headers and HTTP range support. | R |
| FR-303 | The crawler pre-synthesises audio for enrolled sites (post-pilot). | R |
| FR-310 | Player: play/pause, ±10 s, previous/next segment, speed control. | R |
| FR-311 | *(cited only via range `FR-310..316`)* | ? |
| FR-312 | The current segment is highlighted in sync with playback (timing marks or estimation). | R |
| FR-313 | *(cited only via range `FR-310..316`)* | ? |
| FR-314 | First audio within 1 s for pre-generated content. | R |
| FR-315 | A section index lets a listener reach any top-level section without linear traversal. | R |
| FR-316 | *(cited only via range `FR-310..316`)* | ? |
| FR-320 | Speech normalisation expands numbers, dates, currency, percentages and ordinals per the digit policy, and abbreviations from a lexicon. | R |
| FR-321 | Latin-script runs are handled per a configured strategy. | R |
| FR-330 | No placeholder, mask token or zero-width character ever reaches TTS input; presence raises. | R |
| FR-340 | Dzongkha is rendered with a self-hosted, subsetted web font (`font-display: swap`); an OFL-licensed Tibetan font is the default until the DDC Uchen licence is confirmed. [ER-O9 amends] | R |
| FR-341 | The Dzongkha type scale (line height ≈ 2.0, size ≈ 1.3×) is exposed as CSS custom properties; stacked syllables render unclipped. | R |

## FR-4xx — Terminology, memory and review

| ID | Requirement | Status |
|---|---|---|
| FR-400 | Glossary terms are substituted longest-match-first from a versioned termbase, case-sensitive per entry. | R |
| FR-401 | The English term never reaches the model; the approved Dzongkha term always appears in output. | R |
| FR-402 | Glossary compliance rate is reported per batch; the termbase version is exposed in responses. | R |
| FR-403 | DCDD can add, amend, retire and publish termbase entries; changes are audited. | R |
| FR-410 | Every translated segment persists with its source, output, versions and tag-integrity flag (masked; see FR-143). | R |
| FR-411 | Lookup order: approved translation → machine translation → live; subject to the tier gate (FR-510). | R |
| FR-412 | Translations are immutable versions; approval creates a new version and never overwrites; workflow state is separate from content. [ER-14] | P |
| FR-413 | Tier 1 translations can be seeded offline: export with locked placeholders, human approval, and a validated, audited import. [ER-O2] | P |
| FR-420 | Reviewers see segments pending review, filterable by site and tier. | R |
| FR-421 | An approval takes effect without redeployment and supersedes machine output everywhere the segment appears. | R |
| FR-430 | Citizens can report a translation error against a segment. | R |
| FR-431 | Accepted error reports become approved TM entries. | R |
| FR-432 | Error-report intake is rate-limited (10/hour per client hash, 100/day per segment) with a honeypot; limited reports are accepted and silently dropped. [ER-18, ER-O9] | P |

## FR-5xx — Governance

| ID | Requirement | Status |
|---|---|---|
| FR-500 | Content is tiered (1 = most restrictive); tier travels with the request and with the enrolled site's defaults; absent or unparseable tier means Tier 1. | R |
| FR-501 … FR-509 | *(cited only via range `FR-500..511`)* | ? |
| FR-510 | Tier 1 content is never served machine translation from any path (live, cache or TM); without an approved translation it returns `tier_blocked` and source text. The tier gate runs before any lookup. [ER-1] | R |
| FR-511 | Tier 2 content forces glossary use and flags output for review. | R |
| FR-512 | The server resolves the effective tier as the strictest of the site default, site path and selector rules, and the request's hint; a request can make content stricter, never looser; unknown sites are Tier 1. [ER-6] | P |
| FR-520 | A persistent bilingual notice appears on any page carrying machine output. | R |
| FR-521 | The notice cannot be dismissed persistently across pages and carries a working report-an-error affordance. | R |
| FR-522 | Machine output is marked `lang="dz-x-mtfrom-en"`, approved output `lang="dz"`. | R |

## FR-6xx — Operations

| ID | Requirement | Status |
|---|---|---|
| FR-600 | The API is published through the WSO2 gateway (see FR-103 for keyless public routes; wording to confirm). | R |
| FR-601 | Sites are enrolled with a record holding origin, defaults and tier rules. | R |
| FR-602 … FR-609 | *(cited only via range `FR-600..612`)* | ? |
| FR-610 | Health reports each upstream independently. | R |
| FR-611 | Metrics cover cache hit rate, tag integrity, entity preservation, glossary compliance, latency, upstream errors, fallback causes and queue state. | R |
| FR-612 | An operations dashboard shows the metrics. | R |
| FR-620 | Termbase changes, approvals, seed imports, tier-rule and enrolment changes are recorded append-only with actor, action, subject and timestamp. | R |

## NFR — Non-functional

| ID | Requirement | Status |
|---|---|---|
| NFR-100 | `/v1/translate` p95 under 300 ms on a fully cached batch of 64. | R |
| NFR-101 | Performance benchmark (definition to confirm). | ? |
| NFR-102 | Crawler load is bounded and cannot starve live traffic. | R |
| NFR-103 | Performance benchmark (definition to confirm). | ? |
| NFR-104 | Performance benchmark (definition to confirm). | ? |
| NFR-105 | The widget causes no main-thread long task over 50 ms and keeps host input latency p95 under 100 ms on a CPU-throttled low-end profile. [ER-20] | P |
| NFR-106 | The widget retains no removed host nodes; repeated toggling and SPA navigation do not grow memory. [ER-19] | P |
| NFR-200 | Tag integrity ≥ 99% on the frozen evaluation set against the real endpoint; zero malformed markup under every adversarial mock mode. [ER-15 amends] | R |
| NFR-201 | Entity preservation 100%, against both the adversarial mock and the real endpoint. | R |
| NFR-202 | A frozen evaluation set of 500–1,000 real government segments across all tiers is never used for tuning; chrF++ is scored on every model or glossary change. | R |
| NFR-300 | Translated text enters host pages only as text, never parsed as HTML. | R |
| NFR-301 | Allowlists resolve to enrolled site records, never pattern-match request input. | R |
| NFR-302 | Server-side fetching strips cookies and credentials both ways and aborts on apparent authenticated sessions. | R |
| NFR-303 | No reporter or citizen identifier is stored. | R |
| NFR-304 | Personal data is kept out of the model, storage and logs: public pages only; `data-dz-private`/`data-dz-skip`; path normalisation; Tier 2 text persisted or translated only after N distinct clients; logs hold hashes only. [ER-O3] | P |
| NFR-305 | Unapproved machine translations expire after a retention period (proposed 90 days). [ER-O3] | P |
| NFR-400 | Player controls are keyboard-operable with visible focus and 44 × 44 px targets; no autoplay. | R |
| NFR-401 | The widget does not worsen the host page's automated accessibility results (axe-core, zero new violations). | R |
| NFR-410 | Fail open to English: any failure of translation, TTS, cache or upstream leaves a fully working English page; never a broken page, an unresolved spinner or a partly translated block. HTTP responses for translation failures are 200 with source text, never 5xx. | R |
| NFR-411 | Every failure mode in the spec's failure table has an automated fault-injection test in `make check`. [ER-17] | P |
| NFR-412 | Under load beyond capacity, uncached work is shed to a bounded queue rather than delaying responses. | R |
| NFR-413 | With Redis down, a fully cached 64-segment batch stays under p95 800 ms (batched TM read, in-process LRU). [ER-21] | P |
| NFR-500 | Dzongkha-specific logic lives only in `orchestrator/locale/dz.py` and `adapters/widget/src/locale-dz.ts`; CI rejects Tibetan code points, literal or escaped, anywhere else outside `tests/`. [ER-8 amends] | R |
| NFR-502 | The widget works in the Android WebView/Chrome versions in use in Bhutan (pinned matrix: ~90, ~100, current; floors confirmed from pilot analytics). | R |
| NFR-503 | The widget is built only with `tsc` (no downlevel helpers), one pinned minifier and a hash/SRI script; no bundler, framework or polyfills. [ER-O10] | P |

---

## Sign-off

| Role | Name | Date | Notes |
|---|---|---|---|
| SRS owner (DSDD) | | | Replace **R** wording with original; define **?** rows; accept or renumber **P** rows |
