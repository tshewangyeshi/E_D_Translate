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

## Adopted 2026-10-05, and measured

The product owner chose options 1 and 2 below, and confirmed GovTech's model
was not trained with any "do not translate" marker (option 4 is closed).

- xml is the default format, decoded leniently (`orchestrator/pipeline/segment.py`);
- text with no words outside its placeholders is not sent;
- text with inline tags is sent piece by piece, and the tags are put back by
  the service (`orchestrator/service/model_call.py`).

Measured against staging with `--formats live`, same 26 blocks:
**21/26 usable (81%)**, just over the proposed 80% gate. Links, emphasis and
line breaks: 7/7. Remaining failures: four in blocks with a glossary term
(`fee`, masked as a placeholder: the model cannot translate around it), one
where a fee followed by a date lost a placeholder. The price of pieces is
fluency: each piece is translated without the rest of its sentence.

## The pilot's own pages, 2026-10-05

A local copy of https://g2c.tech.gov.bt/g2cportal/ListOfLifeEventComponent
(`tools/g2c_demo/`, content from the portal's own API) was translated through
the widget against staging: the life-events page and ten of its eleven
services (the 300 KB business guide was left out to spare the quota).

Failures found on the way, all but three of them ours, each fixed with tests
(eleven pages before the last two fixes: 259 of 266, 97.4%):

| Seen | Cause | Fix |
|---|---|---|
| "check the judiciary website www.judiciary.gov.bt" | bare `www.` host not masked; the leak scan then rejected the model's faithful copy | `URL` matches bare `www.` hosts |
| "the G2C portal" | the model translated the name by its meaning; the check counted its 2 as lost | digits after a Latin letter are a name |
| "Email ID: …", "Phone number: …", "(www.citizenservices.gov.bt)" | with little else to translate, the model wrote the placeholder in Tibetan letters (`<ཨི་༢/>`) | values after a label or in brackets at the end are not sent |
| "(45mm x 35mm)" | digits before a unit were split by the name rule | only digits *after* a letter are a name |
| "9:00 am to 12:00 pm" | the model wrote "9 to 12" | `9:00` equals `9` |

Second pass: **261 of 264 (98.9%)**. The three left are long academic
paragraphs where the model dropped a citation year ("TANG Yu-fang, 2009"):
content was lost, so English is the right answer. Next: send long paragraphs
sentence by sentence.

Found on the way: the portal's service documents pin Times New Roman on every
run. It has no Tibetan, so on Windows translated text falls back to the very
small Microsoft Himalaya. The portal needs a Dzongkha font for `[lang|="dz"]`
(the copy uses Noto Serif Tibetan, as the portal already loads).

## Options considered

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
