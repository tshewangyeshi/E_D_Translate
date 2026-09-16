# Technical Specification
## Dzongkha Web Translation & Read-Aloud Middleware (dzweb)

Companion to `01-srs.md`. Every section cites the requirements it satisfies. This document is the input to `/plan-eng-review`; expect that skill to challenge it and to add failure-mode and test-matrix detail.

---

## 1. Component map

```
adapters/
  widget/          TypeScript, no framework, no build step   FR-200..215
  proxy/           FastAPI service                            FR-220..224
  cms/             Drupal + WordPress plugins                 FR-230..231

orchestrator/
  api/             HTTP surface, validation, batching         FR-100..101
  pipeline/        ← /guard this directory
    extract.py     node selection, attribute collection       FR-110..114
    segment.py     block grouping, placeholder-isation        FR-120..124
    protect.py     entity masking                             FR-140..141
    glossary.py    termbase substitution                      FR-400..402
    translate.py   upstream NMT client, batching, retry       FR-100
    restore.py     placeholder + entity restoration, validate FR-122..123, 141
    render.py      script rendering, ZWSP, lang attributes    FR-160, 340, 522
    speak.py       TTS normalisation and dispatch             FR-300, 320, 330
  store/
    cache.py       Redis                                      FR-150..151
    tm.py          PostgreSQL translation memory              FR-410..411
    audio.py       object storage                             FR-301..302
  governance/
    tiers.py       tier resolution and enforcement            FR-500..511
    audit.py       audit trail                                FR-620
  review/          reviewer API and UI                        FR-420..421, 430..431
  ops/             health, metrics, enrolment, crawler        FR-152, 600..612
locale/
  dz.py            ALL Dzongkha-specific logic lives here     NFR-500
```

**NFR-500 is enforced structurally**: no module outside `locale/` may contain a Tibetan-script literal or a `dz`-specific branch. A CI check greps for U+0F00–U+0FFF outside `locale/` and `tests/`.

---

## 2. The pipeline

### 2.1 Order of operations

Order matters and is not negotiable. Each stage assumes the previous one ran.

```
extract → segment → protect → glossary → cache lookup → translate → restore → validate → render
                                              ↓ hit
                                          (skip to render)
```

Entity masking (`protect`) runs **before** glossary substitution, because a term may contain a number. Glossary runs **before** cache lookup, because the glossary version is part of the cache key and substitution is deterministic. Validation runs **after** restoration, because it checks the restored output, not the raw model output.

### 2.2 extract.py — node selection (FR-110..114)

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
TRANSLATABLE_ATTRS = ("alt","title","placeholder","aria-label","aria-description")

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

Placeholder tokens use U+27E6/U+27E7 mathematical brackets. They are added to the tokenizer as special tokens during model deployment; if that is not possible, the fallback is `\uE000`-range private-use characters, which the tokenizer treats as unknown but atomic. **Decide this with whoever operates the NMT deployment before writing `restore.py`.**

Segments longer than the model's maximum input split at shad (U+0F0D), double shad (U+0F0E), and English terminal punctuation, never mid-placeholder (FR-124).

### 2.4 protect.py — entity masking (FR-140..141)

```python
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("URL",   re.compile(r"https?://\S+")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("CID",   re.compile(r"\b\d{11}\b")),
    ("REF",   re.compile(r"\b[A-Z]{2,}(?:[/-][A-Z0-9]+){1,}\b")),
    ("CUR",   re.compile(r"\bNu\.?\s?\d[\d,]*(?:\.\d+)?")),
    ("DATE",  re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")),
    ("PCT",   re.compile(r"\b\d+(?:\.\d+)?\s?%")),
    ("NUM",   re.compile(r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b")),
]
```

Order is longest-context-first: `CUR` must precede `NUM` or "Nu. 1,500" masks as currency-symbol-plus-loose-number and the amount can drift. Matching is left-to-right, non-overlapping, first pattern wins.

Masked form: `⟦NUM:3⟧`. The mapping is held per segment, never global.

Restoration failure (FR-141): if any mask token is missing from the model output, the segment is discarded and the **source text** is returned for that segment with `status: "entity_check_failed"`. It is never partially restored.

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

### 2.6 Cache and translation memory (FR-150..151, 410..411)

```python
def cache_key(text: str, src: str, tgt: str, versions: Versions) -> str:
    normalized = unicodedata.normalize("NFC", " ".join(text.split()))
    return sha256("|".join([
        normalized, src, tgt,
        versions.model, versions.glossary, versions.segmentation,
    ]).encode()).hexdigest()
```

`versions.segmentation` is a constant bumped by hand whenever segmentation or masking logic changes in a way that alters output. Forgetting to bump it serves stale translations; the `/review` skill should be told to watch for changes under `pipeline/` that do not bump it.

Lookup order (FR-411): `tm.approved(key)` → `cache.get(key)` → live translate.

**PostgreSQL schema (abbreviated):**

```sql
CREATE TABLE segment (
  key             CHAR(64) PRIMARY KEY,
  source_text     TEXT NOT NULL,
  source_lang     CHAR(2) NOT NULL,
  target_lang     CHAR(2) NOT NULL,
  mt_output       TEXT,
  approved_output TEXT,
  approved_by     TEXT,
  approved_at     TIMESTAMPTZ,
  tier            SMALLINT NOT NULL DEFAULT 1,
  model_version   TEXT NOT NULL,
  glossary_version TEXT NOT NULL,
  tag_integrity   BOOLEAN,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON segment (approved_at) WHERE approved_output IS NOT NULL;
CREATE INDEX ON segment (tier, approved_output) WHERE approved_output IS NULL;

CREATE TABLE error_report (
  id           BIGSERIAL PRIMARY KEY,
  segment_key  CHAR(64) REFERENCES segment(key),
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

Audio is not in Postgres. `sha256(dz_text|voice|speed|tts_version).opus` is the object key; existence in the bucket is the index (FR-301).

### 2.7 render.py — Dzongkha rendering (FR-160, 340..341, 522)

Everything here is render-time and nothing here is persisted.

- Insert U+200B after U+0F0B (tsheg) at break candidates when the `zwsp` render flag is on. Applied to the delivered string only.
- Set `lang="dz-x-mtfrom-en"` on the containing element for machine output; plain `lang="dz"` for human-approved output.
- Emit a CSS custom property block for the Dzongkha type scale rather than hard-coded values, so agencies can adjust: `--dzweb-dz-line-height: 2.0; --dzweb-dz-scale: 1.3;`
- Font: self-hosted subsetted WOFF2, `font-display: swap`, with a documented fallback stack.

### 2.8 speak.py — speech (FR-300, 320..321, 330)

Normalisation runs **on the Dzongkha text**, after rendering artefacts are stripped:

1. Assert no placeholder, mask token, or zero-width character remains. Raise if any do — this is a bug, not an input condition (FR-330).
2. Expand numbers, dates, currency, percentages, ordinals per the digit policy (FR-161, 320).
3. Expand abbreviations from the abbreviation lexicon (a sibling of the termbase, same ownership).
4. Split at shad for segment-level synthesis (FR-130).
5. Handle Latin-script runs per the configured strategy (FR-321). Code-switched playback stitches two audio sources and needs the player to support a playlist per segment — design the player's audio model as a list from the start, even if v1 always has one element.

---

## 3. API contract

Base path `/v1`. Published through WSO2 (FR-600). All responses `application/json` unless noted.

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
                tier:   { type: integer, enum: [1,2,3], default: 1 }
                site:   { type: string, description: enrolled site id }
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
                        id:     { type: string }
                        text:   { type: string }
                        status:
                          type: string
                          enum: [translated, cached, approved, tier_blocked,
                                 entity_check_failed, tag_fallback, upstream_error]
                        cached:        { type: boolean }
                        reviewed:      { type: boolean }
                        tag_integrity: { type: boolean }
        "413": { description: batch or segment too large }
        "429": { description: quota exceeded }

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
                url:    { type: string, format: uri }
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

  /v1/tts:
    post:
      summary: Synthesise a segment (FR-300)
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
      responses:
        "202": { description: queued for review }

  /v1/health:
    get:
      summary: Dependency health (FR-610)
```

### 3.1 Cross-cutting API rules

- **Idempotent and content-addressed.** The same input yields the same output and the same audio URL. Clients may cache aggressively.
- **Never 5xx on a translation failure.** Upstream problems produce a 200 with per-segment `status: upstream_error` and the source text in `text`. This is what makes NFR-410 achievable in the adapters.
- **Partial success is normal.** Some segments cached, some translated, some blocked. Adapters handle per-segment status, never all-or-nothing.
- `ETag` on `/translate` responses derived from model + glossary version, so adapters can revalidate cheaply.

---

## 4. Adapters

### 4.1 Widget (FR-200..215)

Core loop, using a `TreeWalker` and registry keyed by a private symbol on the node:

```ts
const DZ = Symbol("dzweb");
const registry = new Map<string, { node: Text; original: string }>();

function apply(id: string, translated: string) {
  const entry = registry.get(id);
  if (!entry) return;
  entry.node.nodeValue = translated;          // FR-210 — in place, never replaceChild
  const el = entry.node.parentElement;
  el?.setAttribute("lang", "dz-x-mtfrom-en"); // FR-214
}
```

- `MutationObserver` on `document.body`, `{childList: true, subtree: true}`, debounced 150 ms (FR-211). Guard against observing our own mutations: set a re-entrancy flag around `apply`, or the observer will loop.
- Attribute translation is a second pass over `TRANSLATABLE_ATTRS` (FR-113).
- Persistence via a first-party cookie or `localStorage`, origin-scoped (FR-213).
- Every network path wrapped; on any failure the page stays English (FR-215, NFR-410).
- Budget: 15 KB gzipped (FR-201). No framework, no polyfills, no bundler output with helpers. Measure in CI and fail the build on regression.

### 4.2 Proxy (FR-220..224)

Security-critical. `/cso` reviews this before any public exposure.

- Origin allowlist compiled at startup from configuration; `*.gov.bt` is not a wildcard match against arbitrary subdomains supplied at request time — resolve to the enrolled site record (NFR-301).
- Refuse redirects to non-allowlisted hosts. Refuse literal IPs and internal ranges. Refuse anything but GET and HEAD (FR-223).
- Strip `Cookie` and `Authorization` before the upstream request; drop `Set-Cookie` from the response.
- If the upstream response suggests an authenticated session (a `Set-Cookie` with a session name, or a 401/403), abort and serve an explanatory page.
- Rewrite `href`/`src` via `doc.iterlinks()`; anchor-only and `mailto:`/`tel:` links are left alone.
- `hreflang` pair injection (FR-224).

### 4.3 CMS plugin (FR-230..231)

Translate on save into a draft Dzongkha revision, never directly into published content. The review gate (FR-231) is the reason this tier exists; a plugin that publishes machine output directly is the widget with extra steps.

---

## 5. Failure modes

| Failure | Detection | Response | Requirement |
|---|---|---|---|
| NMT upstream down | client timeout/5xx | per-segment `upstream_error`, source text returned | NFR-410 |
| NMT returns mangled placeholders | placeholder multiset mismatch | formatting collapse fallback, `tag_fallback` | FR-122..123 |
| NMT alters a masked entity | mask token missing | discard segment, return source, `entity_check_failed` | FR-141 |
| TTS down | client error | player hidden or disabled with a text explanation; page still translated | NFR-410 |
| Redis down | connection error | bypass cache, serve from TM or live; log, do not fail | NFR-410 |
| Postgres down | connection error | serve from cache only; refuse review writes | NFR-410 |
| Object storage down | 5xx on audio fetch | player degrades to text-only | NFR-410 |
| Tier 1 content submitted for live MT | tier check | `tier_blocked` + source text | FR-510 |
| Host page removes a node the widget registered | node no longer connected | drop the registry entry silently | FR-210 |
| Crawler stampede on a cold cache | queue depth | shed uncached requests, serve English | NFR-412 |

---

## 6. Deployment

Containerised. Orchestrator and proxy are separate deployments with separate scaling and separate security postures — the proxy handles untrusted remote content and should not share a process with the pipeline.

| Environment | Shape |
|---|---|
| dev | single node, PGLite or containerised Postgres, local Redis, MinIO, mocked NMT/TTS |
| staging | production topology at low replica count, real model endpoints, synthetic sites |
| production | multi-node orchestrator, separate proxy tier, managed Postgres, Redis with persistence, object storage behind CDN |

Mocked model endpoints in dev matter more than usual: they let the entity and tag test suites run deterministically and fast, and they let you inject adversarial model behaviour (dropped placeholders, mangled numbers) that a real model produces only occasionally. **Write the adversarial mock before writing `restore.py`.**

---

## 7. Open questions for `/plan-eng-review`

1. Placeholder token strategy — special tokens in the tokenizer, or private-use characters? Depends on whether the NMT deployment can be modified. Blocks `restore.py`.
2. Does the TTS endpoint return timing marks? If not, FR-312 highlight sync needs duration-proportional estimation per segment, which is noticeably worse and should be flagged to the user.
3. Word-level vs sentence-level highlight granularity.
4. Widget persistence: cookie (works across subdomains, consent implications) or `localStorage` (origin-scoped, none).
5. Crawl scheduling: pull (dzweb crawls) or push (CMS notifies on publish)? Push is better and requires agency cooperation.
6. Does the DDC Uchen licence permit web embedding and subsetting?
