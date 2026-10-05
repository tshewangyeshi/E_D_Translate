# S0.1 placeholder survival: first reading (2026-10-05)

Backlog S0.1. Requirements FR-122, FR-140, FR-141, FR-142, FR-401.

**Status: first reading, no decision.** Measured on a synthetic corpus of 26
blocks (`tests/fixtures/s0/synthetic-blocks.json`), not on the pilot
snapshots, which do not exist yet. The go/no-go is the SRS owner's and
GovTech's to make, on real pages.

## How it was measured

`python tools/s0_placeholder_survival.py` against the GovTech staging API
(`dsai_translationapi/1.0.0`), one call per block per format, no retries.
Each block went through the real pipeline (parse, mask entities, substitute
glossary terms), and each answer through the same validation the live service
applies: entities byte-identical, leak scan, terms restored, tags in order.
78 raw responses are in `tests/fixtures/mt-replay/`; `--replay` re-scores them
without calling the API.

## Results

| Format | Usable | Main failure |
|---|---|---|
| wire `⟦1⟧…⟦/1⟧`, `⟦CUR:1⟧` | 6/26 (23%) | markers transliterated into Tibetan script |
| xml `<x1>…</x1>`, `<e1/>`, strict | 7/26 (27%) | tag syntax damaged, identity kept |
| brace `{1}…{/1}`, `{e1}` | 6/26 (23%) | markers dropped or transliterated |
| xml, lenient decoder (replay) | 15/26 (58%) | inline tags unbalanced |
| xml, lenient + skip placeholder-only blocks (replay) | 17/26 (65%) | inline tags unbalanced |

Proposed gate (backlog S0.1): block fallback at most 20%, i.e. at least 80%
usable. **The best variant reaches 65%: below the gate.**

By block type (best variant): form labels 3/3, headings 3/3, paragraphs 5/7,
list items 2/3, paragraphs with a link 2/4, table cells 1/3, paragraphs with
emphasis 0/2.

## What the model does to placeholders

- **Wire markers are transliterated.** `⟦CUR:1⟧` comes back as `ཀུར་ ༡`; the
  digits become Tibetan numerals. Unusable.
- **XML markers keep their identity but lose their syntax:** `<e1>` for
  `<e1/>`, `</x1 >`, `<E4/></X2>`. A decoder that accepts this damage, while
  still requiring every placeholder exactly once, more than doubles survival.
  Occasionally a marker is transliterated (`<ཨི་༡>`); that is not recoverable.
- **Closing tags of inline links and emphasis are dropped** (`<x1>text` with
  no `</x1>`). This is now the largest single cause.
- **A block that is only a placeholder** (a table cell holding just a fee)
  should never be sent; the API even rejected one.
- **Glossary terms masked as placeholders lose their meaning.** "Processing
  `<e1/>`" for "Processing fee" is mistranslated around the placeholder.
- **Without masking** the model rewrites numbers into Tibetan digits and
  reorders them (probe, "Pay Nu. 500 by 30 June 2026"). Masking stays.

## Options to reach the gate (decisions, not done)

1. **Adopt the lenient XML decoder and the placeholder-only skip** in the
   pipeline. Measured gain: 27% → 65%. Touches the guarded
   `orchestrator/pipeline/segment.py`; needs its own tests.
2. **Inline formatting.** For the widget, a block whose tags do not come back
   stays English today (FR-210). Alternatives to evaluate: translate the text
   of each inline run separately, or translate the block without inline tags
   and place the result in the first run. Each changes how links read.
3. **Glossary terms.** Instead of an opaque placeholder, send the approved
   Dzongkha target inline and check it survives, so the model sees a real
   word. Needs the real DCDD termbase to measure.
4. **Ask GovTech** whether the model was trained with any placeholder or
   no-translate convention. A format it knows would beat all three tried here.

## Next measurement

Repeat on the pilot snapshots (S0.1, ~20 pages) once they exist, with the
chosen options applied, and record the decision here.
