# Technical Specification
## Dzongkha Web Translation & Read-Aloud Middleware (dzweb)

Companion to `01-srs.md`. Every section cites the requirements it satisfies.

**Revision 2026-09-16:** updated with the remedies approved in `/plan-eng-review` (`docs/designs/dzweb-eng-review.md`). Review item numbers appear as `[ER-n]` (1–21) and `[ER-On]` (outside voice). Requirements introduced by the review are marked **UNNUMBERED** until the numbered SRS (`docs/00-requirements.md`, backlog S0.3) assigns IDs.

**Pilot scope:** one citizen services portal through the widget. Audio (§2.8, E5), proxy and CMS adapters (§4.2–4.3, E8) and the crawler (S9.2) come after the pilot [ER-4, ER-O6, ER-O7].

---

## 1. Component map

```
adapters/
  widget/          TypeScript → tsc, no bundler/framework     FR-200..215   [ER-O10]
    extract.ts     block extraction, run→node map (PILOT)     FR-110..113, 120  [ER-2, ER-O6]
    locale-dz.ts   browser-side Dzongkha logic only           NFR-500       [ER-8]
  proxy/           FastAPI service (post-pilot)               FR-220..224
  cms/             Drupal + WordPress plugins (post-pilot)    FR-230..231

orchestrator/
  api/             HTTP surface, validation, batching         FR-100..101
  pipeline/        ← /guard this directory
    extract.py     server DOM extraction (proxy/CMS, E8)      FR-110..114   [ER-O6]
    segment.py     segment parsing, placeholder grammar       FR-120..124
    protect.py     entity masking + leak scan                 FR-140..141   [ER-9]
    glossary.py    termbase substitution + fingerprint        FR-400..402   [ER-7]
    translate.py   upstream NMT client, quota, budget         FR-100        [ER-3, ER-O7]
    restore.py     restoration + multiset validation          FR-122..123, 141  [ER-10]
    render.py      script rendering, ZWSP, lang attributes    FR-160, 340, 522
    speak.py       TTS normalisation (post-pilot)             FR-300, 320, 330  [ER-4]
  store/
    cache.py       Redis (masked values only)                 FR-150..151   [ER-12]
    tm.py          PostgreSQL translation memory              FR-410..411   [ER-14]
    audio.py       object storage (post-pilot)                FR-301..302
  queue/
    jobs.py        Postgres SKIP LOCKED job queue + sweeper   UNNUMBERED    [ER-3]
    worker.py      MT worker process                          UNNUMBERED    [ER-3]
  governance/
    tiers.py       tier authority + gate before lookup        FR-500..511   [ER-1, ER-6]
    privacy.py     personal-data controls, retention          UNNUMBERED    [ER-O3]
    audit.py       audit trail                                FR-620
  review/          reviewer API and UI (post-pilot)           FR-420..421, 430..431
  ops/
    fetch.py       the ONLY server-side HTTP fetcher          NFR-301..302  [ER-5]
    seed.py        offline Tier 1 export/import CLI           UNNUMBERED    [ER-O2]
    (health, metrics, enrolment, crawler)                     FR-152, 600..612
locale/
  dz.py            server-side Dzongkha logic                 NFR-500
```

**NFR-500 is enforced structurally** [ER-8]: Dzongkha-specific logic has exactly two homes, `locale/dz.py` (server) and `adapters/widget/locale-dz.ts` (browser). A CI check fails on Tibetan-script code points U+0F00–U+0FFF anywhere else except `tests/`, matching **both literal characters and escape forms** (`\u0F..`, `\x{0F..}`, `&#x0F..;`, and decimal entities `&#3840;`–`&#4095;`). The widget's break rule is kept equal to the Python rule by a shared JSON fixture tested from both sides.

---

## 2. The pipeline

### 2.1 Order of operations

Order matters and is not negotiable. Each stage assumes the previous one ran.

```
resolve tier ─► extract ─► segment ─► protect ─► glossary ─► TIER GATE ─► lookup ─────────► restore ─► validate ─► render
 (server-                                                    │            │ hit (masked)       ▲
  authoritative)                                             │            ▼ miss               │
                                                             │   Tier 1: tier_blocked          │
                                                             │   Tier 2+: translate within ────┘
                                                             │            budget, else pending_mt
                                                             │            + enqueue (worker)
```

Tier is resolved **first** and the tier gate runs **before any lookup** [ER-1, ER-6], so no cache or TM path can bypass FR-510. Entity masking (`protect`) runs **before** glossary substitution, because a term may contain a number. Glossary runs **before** lookup, because the per-segment glossary fingerprint is part of the key [ER-7]. Lookups return **masked** text; restoration always uses the current request's entity map [ER-12]. Validation runs **after** restoration and checks exact placeholder multisets plus a leak scan [ER-9, ER-10].

### 2.2 extract.py — node selection (FR-110..114)

**Pilot note [ER-O6]:** in the pilot, extraction happens in the browser (`adapters/widget/extract.ts`, §4.1). This Python extractor serves proxy, CMS and `/v1/translate/html` and is built in E8. Both extractors must pass **one shared golden fixture set** (`tests/fixtures/extraction/*.json`: HTML → expected units and segments), run by vitest and pytest.

Input: parsed DOM. Output: ordered list of `ExtractionUnit`.

```python
@dataclass(frozen=True)
class ExtractionUnit:
    ref: NodeRef          # opaque handle back to the DOM position
    kind: Literal["text", "tail", "attr"]
    attr: str | None      # set when kind == "attr"
    text: str
```

Exclusion predicate, evaluated by walking ancestors:

```python
SKIP_TAGS = {"script","style","noscript","textarea","code","pre","kbd","samp","var"}
SKIP_CLASSES = {"notranslate", "skiptranslate"}
TRANSLATABLE_ATTRS = ("alt","title","placeholder")
# aria-label / aria-description are NOT translated [ER-18]: no screen reader
# ships a Dzongkha voice, so translating them makes pages worse for
# screen-reader users. Revisit when a Dzongkha screen-reader voice exists.

def translatable(node) -> bool:
    if not node.text or not node.text.strip():
        return False
    for el in ancestors(node):
        if el.tag in SKIP_TAGS: return False
        if el.get("translate") == "no": return False
        if el.get("data-no-translate") is not None: return False
        if SKIP_CLASSES & set(el.get("class","").split()): return False
        if el.get("lang","").startswith("dz"): return False
    return True
```

`<input type="submit">` and `<input type="button">` contribute their `value`. In proxy and CMS modes, `<title>` and the meta/OG description set are added (FR-114); the widget cannot usefully translate these and does not attempt to.

### 2.3 segment.py — block grouping and placeholders (FR-120..124)

The translation unit is the nearest block-level ancestor. Inline descendants collapse to numbered placeholder pairs.

```
<p>Click <a href="/apply"><b>here</b></a> to apply.</p>
    ↓
Segment(
  text = "Click ⟦0⟧here⟦/0⟧ to apply.",
  inlines = {0: InlineSpan(tag="a", attrs={"href": "/apply"}, nested=[Inline("b")])}
)
```

Nested inlines collapse into the outermost placeholder by default. `strictness="preserve-nested"` emits one pair per element; it produces better formatting and worse tag integrity. Default is `collapse`.

**Wire format vs model format [ER-O1].** On the wire (widget \u2194 API) placeholders are `\u27E6N\u27E7\u2026\u27E6/N\u27E7` for inline spans, `\u27E6vN/\u27E7` for void elements, `\u27E6TYPE:n\u27E7` for entities and `\u27E6T:n\u27E7` for glossary terms. Literal `\u27E6`/`\u27E7` in source text are escaped as `\u27E6\u27E6`/`\u27E7\u27E7` before encoding; the server rejects unescaped delimiters and malformed markers. Only `translate.py` converts wire markers to the **model token format** and back.

**The model token format is decided by Sprint 0 (backlog S0.1), not assumed.** Special tokens require embedding changes, which this project cannot make (`01-srs.md`: dzweb does not train models), and private-use characters are likely to become `<unk>` and not survive decoding. Sprint 0 records real WSO2 calls on pilot snapshot blocks for at least three candidate formats, reports survival and fallback rate by cause, and applies a hard go/no-go gate **before `restore.py` (S1.5) is written**.

**Tag mismatch handling [ER-2].** When the placeholder structure of the output does not match the input:
- **Widget:** the block stays English (NFR-410). The widget never removes, replaces or moves nodes (FR-210), so formatting collapse is impossible there.
- **Proxy, CMS and `/v1/translate/html`:** formatting collapse (FR-123) produces valid markup with the segment's formatting applied to the whole.

Segments longer than the model's maximum input split at shad (U+0F0D), double shad (U+0F0E), and English terminal punctuation, never mid-placeholder (FR-124).

### 2.4 protect.py — entity masking (FR-140..141)

```python
AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
         r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("URL",   re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)]")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("CID",   re.compile(r"\b\d{11}\b")),
    ("REF",   re.compile(r"\b[A-Za-z]{2,}(?:[/-][A-Za-z0-9]+)*[/-]\d[A-Za-z0-9]*(?:[/-][A-Za-z0-9]+)*\b")),
    ("CUR",   re.compile(r"(?:\bNu\.?|\bBTN|\bNgultrum)\s?" + AMOUNT)),
    ("DATE",  re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
                         r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + MONTH + r"\.?,?\s+\d{4}\b"
                         r"|\b" + MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b")),
    ("PCT",   re.compile(r"\b\d+(?:\.\d+)?\s?%")),
    ("NUM",   re.compile(r"\d+(?:[.,]\d+)*")),   # catch-all: ANY remaining digit run [ER-9]
]
```

Order is longest-context-first: `CUR` must precede `NUM` or "Nu. 1,500" masks as currency-symbol-plus-loose-number and the amount can drift. Matching is left-to-right, non-overlapping, first pattern wins. **The final `NUM` pattern is a catch-all: no digit run may reach the model unmasked** [ER-9].

> **Why this changed [ER-9]:** the earlier `NUM` pattern `\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b` matched nothing for numbers of four or more digits without commas. Verified: `1500`, `2026` and `17123456` all reached the model unmasked. The patterns above were checked against those cases plus word-month dates, `BTN`/`Ngultrum` amounts, reference numbers, trailing punctuation after URLs and amounts, and percentages.

The pattern set is **data, versioned with the pipeline**: any change alters `pipeline_version` automatically (§2.6) [ER-13].

Masked form: `⟦NUM:3⟧`. The mapping is held per segment and per request, never global, and never persisted [ER-12].

**Restoration validation (FR-141) [ER-10, ER-9].** After restoration, the segment is accepted only if **all** of these hold; otherwise the **source text** is returned with `status: "entity_check_failed"`. It is never partially restored.
1. **Exact multiset:** the entity tokens in the model output equal the input set exactly: every id present once, no duplicates, no unknown ids, no partial or unterminated tokens. The same validator (shared code) checks tag placeholders.
2. **Byte identity:** every restored entity is byte-identical to its source.
3. **Leak scan:** outside restored entities and restored glossary terms, the output contains no ASCII digits, no Tibetan digits (U+0F20–U+0F29), no email address and no URL. This catches numbers the model invents or converts to Tibetan digits.

**Tests:**
- Hypothesis property test: random digit strings, dates and amounts in random English contexts always round-trip byte-identical under every adversarial mock mode.
- Masker recall is measured against hand-labelled pilot snapshots, with a **held-out set** not used for tuning the patterns.

### 2.5 glossary.py — terminology (FR-400..402)

Longest-match-first over a trie built from the published termbase. Matches are case-sensitive for acronyms, case-insensitive otherwise — a flag per entry.

```python
@dataclass(frozen=True)
class Term:
    source: str
    target: str          # DCDD-approved Dzongkha
    case_sensitive: bool
    version_added: str
```

Matched terms mask as `⟦T:n⟧` and restore to `term.target`. The model never sees the English term and cannot produce a variant of it. Glossary compliance rate = matched terms restored / matched terms found.

Each `Term` also carries a stable `term_id` and a `term_version` that changes whenever its target changes.

**Glossary fingerprint [ER-7] (FR-150).** For each segment:

```python
gfp = sha256("|".join(f"{t.term_id}:{t.term_version}"
                      for t in sorted(matched_terms, key=lambda t: t.term_id)))
```

`gfp` replaces the global glossary version in keys (§2.6), so a terminology update invalidates **only segments containing a changed term**, as invariant 6 and S1.7/S7.3 require. A `term_id → keys` index in PostgreSQL finds the affected keys on publish:
- **Machine translations** using a changed term are purged from Redis and TM and re-enqueued through a **rate-capped re-warm**, never all at once.
- **Approved translations** using a changed term move to `needs_recheck` and **stop being served** until re-approved. Tier 1 then returns `tier_blocked`.

### 2.6 Cache and translation memory (FR-150..151, 410..411)

**Everything stored is masked [ER-12].** Keys, Redis values and TM rows hold masked text (`⟦TYPE:n⟧` entities, `⟦T:n⟧` terms, wire tag markers). Real entity values never enter Redis or PostgreSQL. Each response is restored from **that request's** entity map and then validated (§2.4). As a side effect, "Pay Nu. 500" and "Pay Nu. 600" share one entry and one upstream call.

```python
def segment_hash(masked: str) -> str:            # canonical id, a.k.a. segment_key
    normalized = unicodedata.normalize("NFC", " ".join(masked.split()))
    return sha256(normalized.encode()).hexdigest()

def approved_key(seg_hash, src, tgt, pipeline_version, gfp) -> str:
    return sha256("|".join([seg_hash, src, tgt, pipeline_version, gfp]).encode()).hexdigest()

def machine_key(seg_hash, src, tgt, pipeline_version, gfp, model_version) -> str:
    return sha256("|".join([seg_hash, src, tgt, pipeline_version, gfp, model_version]).encode()).hexdigest()
```

Normalising whitespace and NFC is safe **only** because entities are masked before hashing. Whitespace between runs is always taken from the request's own source, never from storage [ER-12].

**`pipeline_version` is derived, not hand-bumped [ER-13].** At startup it is computed as `sha256(pattern data ‖ segmentation rules ‖ masked output of the golden fixture set)`. Any change that alters masking or segmentation output changes the version automatically; refactors that don't change output leave it alone. CI prints the old and new version in the PR.

**Tier gate and lookup order (FR-411, FR-510) [ER-1]:**

```
effective_tier = resolve_tier(site, path, selectors, request_tier)     # §2.9, ER-6
if tier == 1:  tm.approved(approved_key)                    → else tier_blocked + source
else:          redis/tm approved(approved_key)
               → redis/tm machine(machine_key)
               → live translate within budget (§2.10)       → else pending_mt + enqueue
```

Every Redis value carries its origin (`approved` | `mt`). A Tier 1 request **rejects an `mt` value even on a cache hit**. Test `test_fr510_tier1_never_served_cached_mt` fills the cache from a Tier 2 request and asserts the Tier 1 request gets `tier_blocked`. One batched TM query per request (`WHERE key = ANY($1)`) serves all segments [ER-21].

**Model version** comes from deployment configuration pinned alongside the WSO2 endpoint. If WSO2 exposes a model-version header, the adapter compares the two and refuses to serve on mismatch.

**PostgreSQL schema (abbreviated) [ER-14]:**

```sql
-- One row per distinct masked source. No tier column: tier belongs to the
-- request, not the content (the same sentence appears on Tier 1 and Tier 2 pages).
CREATE TABLE segment (
  segment_key      CHAR(64) PRIMARY KEY,          -- segment_hash(masked source)
  masked_source    TEXT NOT NULL,
  source_lang      CHAR(2) NOT NULL,
  target_lang      CHAR(2) NOT NULL,
  pipeline_version TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable: every machine output and every human approval is a new row.
CREATE TABLE translation_version (
  id               BIGSERIAL PRIMARY KEY,
  segment_key      CHAR(64) NOT NULL REFERENCES segment(segment_key),
  lookup_key       CHAR(64) NOT NULL,             -- approved_key or machine_key
  origin           TEXT NOT NULL CHECK (origin IN ('mt','human')),
  masked_target    TEXT NOT NULL,
  gfp              CHAR(64) NOT NULL,
  model_version    TEXT,                          -- NULL for origin = 'human'
  author           TEXT,                          -- reviewer ref for human; NULL for mt
  tag_integrity    BOOLEAN,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON translation_version (lookup_key);

-- Mutable workflow state; points at the current approved version.
CREATE TABLE review_item (
  segment_key      CHAR(64) PRIMARY KEY REFERENCES segment(segment_key),
  state            TEXT NOT NULL CHECK (state IN
                     ('pending_review','approved','needs_recheck','rejected')),
  current_version  BIGINT REFERENCES translation_version(id),
  site_id          TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE glossary_hit (                       -- term_id → keys index [ER-7]
  term_id          TEXT NOT NULL,
  lookup_key       CHAR(64) NOT NULL,
  PRIMARY KEY (term_id, lookup_key)
);

CREATE TABLE error_report (
  id           BIGSERIAL PRIMARY KEY,
  segment_key  CHAR(64) REFERENCES segment(segment_key),
  suggested    TEXT,
  reporter_ref TEXT,               -- opaque; never an identifier  NFR-303
  status       TEXT NOT NULL DEFAULT 'open',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (            -- FR-620
  id        BIGSERIAL PRIMARY KEY,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  subject   TEXT NOT NULL,
  detail    JSONB,
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**When `pipeline_version` changes**, approved translations are migrated, not orphaned. The new masker and segmenter run on each approved row's `masked_source`, treating existing placeholders as opaque:
- **Output unchanged:** the row is re-keyed to the new version.
- **Output changed** (new entities detected, or a narrowed pattern no longer produces an existing placeholder): the `review_item` moves to `needs_recheck`.

Machine rows are not migrated; their keys miss and they re-warm through the rate-capped queue. Tests cover widened, narrowed and new patterns.

Audio is not in Postgres. `sha256(dz_text|voice|speed|tts_version).opus` is the object key; existence in the bucket is the index (FR-301). *(Post-pilot, E5.)*

### 2.7 render.py — Dzongkha rendering (FR-160, 340..341, 522)

Everything here is render-time and nothing here is persisted.

- Insert U+200B after U+0F0B (tsheg) at break candidates when the `zwsp` render flag is on. Applied to the delivered string only. In widget mode this runs in `adapters/widget/locale-dz.ts` on the string written to `nodeValue`; in proxy/CMS mode it runs here. Both implement one rule, checked by the shared fixture [ER-8].
- Set `lang="dz-x-mtfrom-en"` on the containing element for machine output; plain `lang="dz"` for human-approved output.
- Emit a CSS custom property block for the Dzongkha type scale rather than hard-coded values, so agencies can adjust: `--dzweb-dz-line-height: 2.0; --dzweb-dz-scale: 1.3;`
- Font: self-hosted subsetted WOFF2, `font-display: swap`, with a documented fallback stack. **Until the DDC Uchen web-embedding licence is confirmed, ship an OFL-licensed Tibetan font (e.g. Noto Serif Tibetan; licence confirmed by GovTech) as the default**, so Dzongkha never renders as missing-glyph boxes [ER-O9].

### 2.8 speak.py — speech (FR-300, 320..321, 330)

**Post-pilot [ER-4].** Audio (E5) is built after the translation pilot. The FR-330 storage guards (no placeholder, mask token or zero-width character in cache, TM or anything TTS would read) are enforced **now**, in E1 and E6, so speech can be added later without rework.

Normalisation runs **on the Dzongkha text**, after rendering artefacts are stripped:

1. Assert no placeholder, mask token, or zero-width character remains. Raise if any do — this is a bug, not an input condition (FR-330).
2. Expand numbers, dates, currency, percentages, ordinals per the digit policy (FR-161, 320).
3. Expand abbreviations from the abbreviation lexicon (a sibling of the termbase, same ownership).
4. Split at shad for segment-level synthesis (FR-130).
5. Handle Latin-script runs per the configured strategy (FR-321). Code-switched playback stitches two audio sources and needs the player to support a playlist per segment — design the player's audio model as a list from the start, even if v1 always has one element.

### 2.9 tiers.py — tier authority (FR-500..511) [ER-6]

The server decides the tier. The request can only make content **stricter**:

```python
def resolve_tier(site: EnrolledSite | None, path: str, request_tier: int | None,
                 matched_selector_tier: int | None) -> int:
    if site is None:
        return 1                                    # unknown site → most restrictive
    candidates = [site.default_tier,
                  site.path_rules.tier_for(normalise_path(path)),
                  matched_selector_tier,            # widget reports which configured selector matched
                  request_tier]
    valid = [t for t in candidates if t in (1, 2, 3)]
    return min(valid) if valid else 1               # tier 1 is most restrictive; lowest number wins
```

Tier numbers run from 1 (most restrictive) upward, so "the strictest tier wins" means the **lowest** number. Absent or unparseable input resolves to Tier 1 (S3.1).

- **Rules live in the enrolled site record** (path patterns and CSS selectors), maintained by admins and audited (FR-620).
- **`GET /v1/config?site=…`** delivers the site's Tier 1 selectors to the widget. **If config is unavailable, the widget offers no translation** and the page stays English (NFR-410), because a widget without selectors could send Tier 1 content as Tier 2.
- **Review-item creation is capped** per site per day, so forged requests can't flood the review queue.
- **Tests:** a request claiming Tier 2 for a path or selector the site marks Tier 1 → `tier_blocked`; unknown site → Tier 1; config fetch failure → no translation requests.

### 2.10 translate.py and queue/ — budget, queue, quota [ER-3, ER-O7]

**Live budget.** `/v1/translate` attempts live MT only for Tier 2+ misses, within a per-request budget (proposed 1.5 s total, UNNUMBERED). Segments not finished in budget return `status: "pending_mt"` with source text and are enqueued. The widget re-requests `pending_mt` segments **once**, after about 8 s. If they are still pending, the page stays English for this view.

**Job queue** (PostgreSQL, no new service):
- Jobs are claimed with `SELECT … FOR UPDATE SKIP LOCKED` in short transactions, with a partial index on pending rows ordered by priority and age.
- **Uniqueness applies to active jobs only** (a partial unique index on `machine_key WHERE state IN ('pending','running')`), so an invalidated key can be enqueued again.
- A **visibility-timeout sweeper** returns jobs claimed by a crashed worker to `pending`.
- Enqueue is skipped when a current `translation_version` already exists for the key; this dedupe matters when Redis is down [ER-21].
- Load shedding **enqueues** instead of dropping, with a bounded queue depth (NFR-412).

**Quota manager.** A token bucket sized to the WSO2 limits measured in Sprint 0 (rps, max batch, max input length) reserves **at least 50% of capacity for the worker**. Live attempts only use leftover tokens; with none left, they skip straight to enqueue. Traffic spikes therefore cannot starve the worker, which would otherwise leave the cache permanently cold.

**Pre-warm before pilot launch:** every enrolled pilot page is translated offline from the Sprint 0 snapshots through the same worker. The pilot has no crawler.

### 2.11 privacy.py — personal data (UNNUMBERED) [ER-O3]

Entity masking protects numbers, IDs, emails and URLs, **not names or addresses**. A citizen services portal can display both. These controls keep personal data out of WSO2, TM, logs and the review queue:

- **Public pages only** in the pilot enrolment.
- The widget does not run on pages marked `data-dz-private` and skips regions marked `data-dz-skip`.
- **Path normalisation:** numeric and ID-like path segments become `:id` before storage.
- **N distinct clients:** a Tier 2 segment is neither persisted nor sent to MT until it has been seen from at least N distinct clients (proposed 3), counted with a salted, daily-rotated client hash. One-off personalised strings therefore never leave the request.
- **Retention:** unapproved machine rows expire (proposed 90 days); approved rows are kept.
- **Logs** hold `segment_key` hashes only, never text.
- **`/cso` gates** this component.

---

## 3. API contract

Base path `/v1`. All responses `application/json` unless noted.

**Two classes of route [ER-O4] (FR-600):**
- **Public widget routes** (`/v1/translate`, `/v1/config`, `/v1/feedback`): keyless, because a browser cannot keep a key secret. Published through WSO2 as passthrough if FR-600 requires every public API to go through the gateway. Protected by the enrolled-origin allowlist, per-origin and per-IP rate limits, and request size limits. Designed as CORS simple requests where possible, to avoid a preflight on slow 4G.
- **Server-to-server routes** (`/v1/translate/html`, CMS, proxy, admin): WSO2 per-consumer keys and quotas.

*Confirm FR-600's exact wording with GovTech.*

```yaml
openapi: 3.1.0
info: { title: dzweb orchestrator, version: "0.1" }

paths:
  /v1/translate:
    post:
      summary: Translate a batch of segments (FR-100)
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [source, target, segments]
              properties:
                source: { type: string, enum: [en] }
                target: { type: string, enum: [dz] }
                tier:
                  type: integer
                  enum: [1,2,3]
                  default: 1
                  description: >
                    Hint only. The server's resolved tier wins; a request can
                    make content stricter, never looser (§2.9, ER-6).
                site:   { type: string, description: enrolled site id }
                path:   { type: string, maxLength: 512, description: "location.pathname only; normalised server-side (§2.11)" }
                segments:
                  type: array
                  maxItems: 64
                  items:
                    type: object
                    required: [id, text]
                    properties:
                      id:     { type: string, maxLength: 64 }
                      text:   { type: string, maxLength: 5000 }
                      format: { type: string, enum: [text, placeholder], default: text }
                      selector_tier: { type: integer, enum: [1,2,3], description: "tier of the configured selector this block matched, if any (§2.9)" }
      responses:
        "200":
          content:
            application/json:
              schema:
                type: object
                properties:
                  model_version:    { type: string }
                  glossary_version: { type: string }
                  segments:
                    type: array
                    items:
                      type: object
                      properties:
                        id:          { type: string }
                        segment_key: { type: string, description: "masked-source hash; used by /v1/feedback (ER-O9)" }
                        text:        { type: string }
                        status:
                          type: string
                          description: >
                            Final: translated, tier_blocked, entity_check_failed,
                            tag_fallback, upstream_error. Non-final: pending_mt.
                          enum: [translated, pending_mt, tier_blocked,
                                 entity_check_failed, tag_fallback, upstream_error]
                        origin:
                          type: string
                          enum: [mt, human]
                          description: "set only when status = translated (ER-14)"
                        tag_integrity: { type: boolean }
        "413": { description: batch or segment too large }
        "429": { description: rate limit exceeded for this origin or client }

  /v1/translate/html:
    post:
      summary: Translate a whole document (FR-101)
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [html]
              properties:
                html:   { type: string }
                url:
                  type: string
                  format: uri
                  description: "Metadata only (link rewriting, logging). The server NEVER fetches it (ER-5)."
                target: { type: string, default: dz }
                tier:   { type: integer, default: 1 }
      responses:
        "200":
          content:
            application/json:
              schema:
                type: object
                properties:
                  html: { type: string }
                  stats:
                    type: object
                    properties:
                      segments:        { type: integer }
                      cache_hits:      { type: integer }
                      tag_fallbacks:   { type: integer }
                      entity_failures: { type: integer }

  /v1/config:
    get:
      summary: Widget configuration for an enrolled site (UNNUMBERED, ER-6)
      parameters:
        - { name: site, in: query, required: true, schema: { type: string } }
      responses:
        "200":
          headers:
            Cache-Control: { description: "public, max-age=300" }
          content:
            application/json:
              schema:
                type: object
                properties:
                  config_version: { type: string }
                  tier1_selectors: { type: array, items: { type: string } }
                  private_selectors: { type: array, items: { type: string } }
                  enabled:        { type: boolean }
        "404": { description: "site not enrolled — widget stays English" }

  /v1/tts:
    post:
      summary: Synthesise a segment (FR-300) — post-pilot (ER-4)
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [text]
              properties:
                text:  { type: string, maxLength: 1000 }
                voice: { type: string, default: "dz-default" }
                speed: { type: number, default: 1.0, minimum: 0.5, maximum: 2.0 }
      responses:
        "200":
          content:
            application/json:
              schema:
                type: object
                properties:
                  audio_url:   { type: string }
                  duration_ms: { type: integer }
                  marks:
                    type: array
                    description: optional word timings for highlight sync (FR-312)
                    items:
                      type: object
                      properties:
                        time_ms: { type: integer }
                        start:   { type: integer }
                        end:     { type: integer }

  /v1/audio/{hash}.opus:
    get:
      summary: Fetch synthesised audio (FR-302). Immutable; supports Range.
      responses:
        "200": { content: { audio/ogg: {} } }
        "206": { description: partial content }

  /v1/feedback:
    post:
      summary: Report a translation error (FR-430)
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [segment_key]
              properties:
                segment_key: { type: string }
                suggested:   { type: string, maxLength: 5000 }
                website:     { type: string, description: "honeypot; must be empty (ER-18)" }
      responses:
        "202": { description: "queued for review; also returned (and silently dropped) when rate-limited or honeypot filled" }

  /v1/health:
    get:
      summary: Dependency health (FR-610)
```

### 3.1 Cross-cutting API rules

- **Deterministic for a given state, not constant over time [ER-O5].** The same input yields the same output while nothing changes. A `pending_mt` segment later becomes `translated`, and an approval or a glossary change alters the result without any model version change.
- **HTTP caching [ER-O5]:**
  - `ETag` is a hash of the **response body**.
  - A response containing **any non-final segment** (`pending_mt`) gets `Cache-Control: no-store`.
  - An all-final response gets `Cache-Control: private, max-age=60` with revalidation.
  - Test: a pending response is never answered with 304 after its job completes.
- **Never 5xx on a translation failure.** Upstream problems produce a 200 with per-segment `status: upstream_error` and the source text in `text`. This is what makes NFR-410 achievable in the adapters. This includes PostgreSQL or Redis being down during the tier-gate lookup: fail closed to source text, still 200 [ER-17].
- **Partial success is normal.** Some segments cached, some translated, some blocked. Adapters handle per-segment status, never all-or-nothing.
- **Feedback limits [ER-18]:** 10 reports per hour per client hash, 100 per day per segment, plus a honeypot field. No reporter identifier is stored (NFR-303).

---

## 4. Adapters

### 4.1 Widget (FR-200..215)

**A segment is a block; a block spans many text nodes [ER-2].** `<p>Click <a><b>here</b></a> to apply.</p>` is one segment written back into **three** text nodes. State is keyed by node, never by string id, so removed nodes can be garbage-collected [ER-19]:

```ts
interface TextState  { original: string; lastWritten: string | null }
interface BlockState { runs: Text[]; gen: number; sentText: string | null; lang?: string }

const textState  = new WeakMap<Text, TextState>();       // ER-11, ER-19
const blockState = new WeakMap<Element, BlockState>();
const inFlight   = new Map<string, WeakRef<Element>>();   // request id → block; pruned on response or dead ref

function apply(block: Element, gen: number, runs: string[], origin: "mt" | "human") {
  const st = blockState.get(block);
  if (!st || st.gen !== gen || !dzOn()) return;                         // stale response guard
  if (runs.length !== st.runs.length) return;                           // structure re-checked client-side
  if (st.runs.some(n => !n.isConnected)) return;
  if (st.runs.map(n => n.nodeValue).join(" ") !== st.sentText) return; // host changed text mid-flight
  st.runs.forEach((node, i) => {
    const text = localeDz.insertBreaks(runs[i]);                        // render-time ZWSP, locale-dz.ts (ER-8)
    textState.get(node)!.lastWritten = text;
    node.nodeValue = text;                                              // FR-210: in place, never replaceChild
  });
  block.setAttribute("lang", origin === "human" ? "dz" : "dz-x-mtfrom-en");  // FR-214, FR-522
  block.setAttribute("data-dz-on", "");
}
```

**Tag mismatch in the widget:** if the response's run count or marker order doesn't match the block's nodes, the block **stays English**. The widget never removes or restructures nodes to "collapse" formatting [ER-2].

**Observing the host page [ER-11]:**
- `MutationObserver` on `document.body` with `{ childList: true, characterData: true, subtree: true }`. React and Vue update text by assigning `nodeValue`, which is a **characterData** mutation that `childList` alone would miss.
- A characterData record whose node value **differs from `lastWritten`** is a host change: update `original`, bump the block's `gen`, and re-extract the block.
- A record whose value **equals `lastWritten`** is the widget's own write and is ignored. There is no re-entrancy flag: mutation records arrive asynchronously, after any flag would already be cleared.
- **Toggle back** writes `original` only where the current value still equals `lastWritten`. Otherwise the host has re-rendered and its text stays. Without this, a fee the host changed from Nu. 500 to Nu. 600 would be reverted to the stale Nu. 500.
- `childList` removals prune in-flight work; `WeakMap` state is collected with the nodes [ER-19].

**Extraction cost on low-end Android [ER-20]:**
- The initial pass walks blocks in **viewport order** and yields to the main thread every ~8 ms (`scheduler.yield()`, `setTimeout` fallback).
- Mutations add only the nearest block of each added or changed node to a **dirty set**, debounced 150 ms (FR-211). There is no whole-document rescan.
- `IntersectionObserver` defers off-screen blocks until they approach the viewport.
- An SPA route change produces one batch (S4.2).

**Hydration safety [ER-16]:** auto-translate from a saved preference starts only after the `load` event, then `requestIdleCallback` (with `setTimeout` fallback), then two animation frames with no host `childList` mutations under the target blocks. This avoids React/Vue SSR hydration mismatches.

**Config first [ER-6]:** the widget fetches `GET /v1/config` before offering translation. On failure, 404 or `enabled: false`, it shows no toggle and the page stays English. Blocks matching `private_selectors` or `[data-dz-skip]` are never extracted, and pages marked `data-dz-private` load nothing [ER-O3].

**Other rules:**
- **Attributes:** a second pass over `alt`, `title` and `placeholder` only (FR-113), with originals in a `WeakMap`, written via `setAttribute` and restored exactly on toggle. `aria-label` and `aria-description` are never translated [ER-18].
- **Persistence** via a first-party cookie or `localStorage`, origin-scoped (FR-213).
- **Networking:** every network path is wrapped; on any failure the page stays English (FR-215, NFR-410). `pending_mt` segments are re-requested once after ~8 s.
- **Text insertion:** translated text only ever enters the page through `nodeValue` or `setAttribute`, never `innerHTML`, so a script-bearing response cannot execute (S4.4).

**Build [ER-O10].** Browsers can't run TypeScript, so "no build step" means **no bundler, no framework, no polyfills**. The only allowed pipeline is:
1. `tsc` targeting ES2017 with `importHelpers: false`. A CI grep fails on emitted helper functions (`__awaiter`, `__extends`, `__rest`, …).
2. One pinned minifier with bundling off.
3. A script that writes the content-hashed filename and the SRI hash.

**Budget:** 15 KB gzipped (FR-201), measured by CI on the final hashed file; the build fails on regression.

### 4.2 Proxy (FR-220..224)

Security-critical. `/cso` reviews this before any public exposure. Post-pilot (E8).

**All server-side fetching goes through `ops/fetch.py` [ER-5]**, shared by the proxy and the crawler (S9.2). No other module may open an outbound HTTP connection to page content; a CI check enforces this. `/v1/translate/html` never fetches its `url`. `fetch.py` implements:

- Origin allowlist compiled at startup from configuration; `*.gov.bt` is not a wildcard match against arbitrary subdomains supplied at request time — resolve to the enrolled site record (NFR-301).
- Refuse redirects to non-allowlisted hosts. Refuse literal IPs and internal, loopback, link-local and metadata ranges (e.g. 169.254.169.254). **The IP is checked after DNS resolution and again on the connected socket**, which defeats DNS rebinding. Refuse anything but GET and HEAD (FR-223).
- Response size and total time caps.
- Strip `Cookie` and `Authorization` before the upstream request; drop `Set-Cookie` from the response.
- If the upstream response suggests an authenticated session (a `Set-Cookie` with a session name, or a 401/403), abort and serve an explanatory page.
- Rewrite `href`/`src` via `doc.iterlinks()`; anchor-only and `mailto:`/`tel:` links are left alone.
- `hreflang` pair injection (FR-224).

### 4.3 CMS plugin (FR-230..231)

Translate on save into a draft Dzongkha revision, never directly into published content. The review gate (FR-231) is the reason this tier exists; a plugin that publishes machine output directly is the widget with extra steps.

---

## 5. Failure modes

Every row has a test in `tests/fault/` (run by `make check` against real PostgreSQL, Redis and a controllable WSO2 mock), named `test_nfr410_*` or after the listed requirement. Each asserts HTTP 200, source text where applicable, and an emitted metric [ER-17].

| Failure | Detection | Response | Requirement |
|---|---|---|---|
| NMT upstream down | client timeout/5xx | per-segment `upstream_error`, source text returned | NFR-410 |
| NMT returns mangled placeholders | placeholder multiset mismatch | widget: block stays English; proxy/CMS: formatting collapse; `tag_fallback` | FR-122..123 [ER-2] |
| NMT alters, duplicates or invents an entity token | exact multiset / byte identity check | discard segment, return source, `entity_check_failed` | FR-141 [ER-10] |
| NMT invents or converts a number | leak scan | discard segment, return source, `entity_check_failed` | FR-140 [ER-9] |
| TTS down *(post-pilot)* | client error | player hidden or disabled with a text explanation; page still translated | NFR-410 |
| Redis down | connection error | one batched TM query; no enqueue for keys already translated; rate-capped enqueue; in-process LRU; log, do not fail | NFR-410 [ER-21] |
| Postgres down | connection error | serve from Redis only (origin check still applied; Tier 1 without cached `human` origin → source); refuse review writes; never 5xx | NFR-410, FR-510 [ER-1] |
| Object storage down *(post-pilot)* | 5xx on audio fetch | player degrades to text-only | NFR-410 |
| Tier 1 content requested (any path: live, cache, TM) | tier gate before lookup | `tier_blocked` + source text unless approved | FR-510 [ER-1] |
| Request under-declares tier | server tier resolution | server tier wins | FR-510 [ER-6] |
| Config endpoint down | widget fetch fails | no toggle offered; page stays English | NFR-410 [ER-6] |
| Host page removes a node the widget registered | `childList` removal / dead `WeakRef` | prune in-flight work; `WeakMap` state collected | FR-210 [ER-19] |
| Host changes text while Dzongkha is on | characterData ≠ `lastWritten` | update original, re-extract block | FR-212 [ER-11] |
| Response arrives after toggle-back or host change | generation / sent-text mismatch | discard response | FR-210 [ER-11] |
| Worker crashes mid-job | visibility timeout | sweeper returns job to pending | UNNUMBERED [ER-3] |
| Traffic spike exhausts live quota | token bucket empty | skip live MT, enqueue; worker keeps reserved share | NFR-412 [ER-O7] |
| Stampede on a cold cache / termbase publish | queue depth | shed live attempts to the queue (enqueue, not drop); rate-capped re-warm | NFR-412 [ER-3, ER-7] |
| Fetch redirected to internal IP / DNS rebinding | IP check after resolve and on connect | refuse, log | NFR-301 [ER-5] |
| Personalised string on an enrolled page | N-distinct-clients threshold | not persisted, not sent to MT | UNNUMBERED [ER-O3] |
| Offline seed row with broken placeholders | import multiset validation | row rejected with reason | UNNUMBERED [ER-O2] |

---

## 6. Deployment

Containerised. Orchestrator and proxy are separate deployments with separate scaling and separate security postures — the proxy handles untrusted remote content and should not share a process with the pipeline. The MT worker (`queue/worker.py`) is a separate process entrypoint in the orchestrator image, scaled independently [ER-3].

| Environment | Shape |
|---|---|
| dev | single node, PGLite or containerised Postgres, local Redis, MinIO, mocked NMT/TTS |
| staging | production topology at low replica count, real model endpoints, synthetic sites |
| production | multi-node orchestrator, separate proxy tier, managed Postgres, Redis with persistence, object storage behind CDN |

Mocked model endpoints in dev matter more than usual: they let the entity and tag test suites run deterministically and fast, and they let you inject adversarial model behaviour (dropped placeholders, mangled numbers) that a real model produces only occasionally. **Write the adversarial mock before writing `restore.py`.**

---

## 7. Open questions

Resolved by `/plan-eng-review` (2026-09-16):
- ~~Placeholder token strategy~~: decided by **Sprint 0** measurement against the real WSO2 endpoint (§2.3, S0.1) [ER-O1].

Still open:
1. Does the TTS endpoint return timing marks? If not, FR-312 highlight sync needs duration-proportional estimation per segment, which is noticeably worse and should be flagged to the user. *(Post-pilot.)*
2. Word-level vs sentence-level highlight granularity. *(Post-pilot.)*
3. Widget persistence: cookie (works across subdomains, consent implications) or `localStorage` (origin-scoped, none).
4. Crawl scheduling: pull (dzweb crawls) or push (CMS notifies on publish)? Push is better and requires agency cooperation. *(Post-pilot.)*
5. Does the DDC Uchen licence permit web embedding and subsetting? Until confirmed, the OFL fallback font ships (§2.7).
6. **FR-600 wording:** must every public API go through WSO2, and can WSO2 publish keyless passthrough routes? (§3) [ER-O4]
7. **WSO2 limits:** rps, max batch, max input length, p95 latency. Measured in Sprint 0 (S0.2) [ER-O7].
8. **Reviewers:** who at DCDD approves the offline Tier 1 seed, and on what timeline (S7.0) [ER-O2].
9. **Numbers to confirm:** live budget (1.5 s), re-request delay (8 s), N distinct clients (3), retention (90 days), worker quota share (≥50%).
10. **The numbered SRS** (`docs/00-requirements.md`) must define every UNNUMBERED item above (S0.3) [ER-O8].
