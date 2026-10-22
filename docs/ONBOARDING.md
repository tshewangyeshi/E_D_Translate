# Onboarding: your first week on dzweb

Welcome. This guide gets you from zero to your first reviewed change. Read it
top to bottom once; come back to the glossary as often as you need.

## 1. What dzweb is, in one minute

dzweb lets a Bhutanese government website be read in Dzongkha. A citizen opens
a page, clicks **རྫོང་ཁ**, and the page's English text is replaced, in place,
by Dzongkha. Images, links, forms and layout stay exactly as they were. Click
**English** and the original comes back. The pilot site is the G2C citizen
services portal (`https://citizenservices.gov.bt/g2cportal`).

```
Reader's browser                     dzweb server                 GovTech
┌───────────────────────┐            ┌──────────────┐             ┌─────────────┐
│ government page       │   English  │ API + worker │   English   │ translation │
│  + widget (རྫོང་ཁ btn) │ ─────────► │ + PostgreSQL │ ──────────► │ model (NMT) │
│                       │ ◄───────── │ + Redis      │ ◄────────── │ via WSO2    │
└───────────────────────┘  Dzongkha  └──────────────┘   Dzongkha  └─────────────┘
```

Three parts:

- **The widget** (`adapters/widget/`, TypeScript): runs in the reader's
  browser. Finds the page's text, sends it, writes the Dzongkha back.
- **The orchestrator** (`orchestrator/`, Python): the server. Protects numbers,
  links and IDs from the model, uses approved translations before machine
  ones, caches, queues, rate-limits, and records everything that matters.
- **The translation model**: GovTech's machine translator, which we call over
  an API. We do not train or host it.

Approved translations (written or checked by DCDD, the Dzongkha Development
Commission's language experts) always win over machine translation. Legal and
fee text (Tier 1) is never machine-translated at all.

## 2. Read these, in this order

| When | What | Why |
|---|---|---|
| Day 1 | this guide; `CLAUDE.md` (repo root) | the shape of the project and how to run it |
| Day 1 | `docs/CLAUDE.md` | the **non-negotiable rules** and Dzongkha facts you will not know |
| Day 1-2 | `docs/00-requirements.md` | every requirement has an ID (FR-140, NFR-410…). They appear in commits, test names and comments: learn the common ones |
| Day 3+ | `docs/02-technical-spec.md` | how it is built. Long: read the section for the code you are working on, not all at once |
| Any time | `docs/03-backlog.md`, `TODOS.md` | what is done, what is next |

## 3. The rules that are never broken

From `docs/CLAUDE.md`, in short. A change that breaks one is a bug, however
good it is otherwise.

1. **Numbers, dates, amounts, IDs, emails and links come out exactly as they
   went in.** (FR-140)
2. **When anything fails, the page stays in English.** Never a broken page or a
   half-translated block. (NFR-410)
3. **The widget never creates, removes or moves page elements.** It only
   changes the text inside existing text nodes. (FR-210)
4. **Tier 1 content (fees, legal text) is never machine-translated.** (FR-510)
5. **Never use real citizen data** in code, tests, fixtures or chat tools.
   Invent it; ready-made synthetic IDs are in
   `tests/fixtures/synthetic-entities.json`.
6. **Secrets never enter the repository.** Not in code, not in commits, not in
   screenshots. The repository is public.

## 4. Set up and run it (day 1-2)

You need: Python 3.11+, Node.js 20+, Git, Docker Desktop. On Windows, use Git
Bash for the commands below.

```bash
git clone https://github.com/tshewangyeshi/E_D_Translate.git && cd E_D_Translate
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python
(cd adapters/widget && npm ci && npx playwright install chromium firefox webkit)
docker compose up -d                 # a throwaway PostgreSQL and Redis
```

Then, in this order:

```bash
python tools/demo.py                 # the whole pipeline end to end, no Docker needed: read its output
python tools/check.py                # every test and gate; must pass before any change is merged
```

`tools/check.py` takes several minutes and runs about 22 checks: lint, types,
Python tests, widget tests, browser tests, quality gates. If something fails on
a fresh clone, tell your mentor: that is a bug in the setup, not your fault.

**You work with the mock translator.** The real GovTech translator needs
credentials you do not need yet. The mock answers `DZ:` + the English, which is
enough for almost all development:

```bash
export DZWEB_TRANSLATOR=mock DZWEB_ALLOW_MOCK_TRANSLATOR=1
```

The full list of settings, and how to run the API and worker locally, is in the
root `CLAUDE.md`.

## 5. Where things are

```
adapters/widget/src/       the widget
  extract.ts               reads the page into segments (start here)
  apply.ts                 writes translations back into the same text nodes
  widget.ts                the flow: config, translate, toggle, stale answers
  locale-dz.ts             everything Dzongkha-specific in the widget
  notice.ts                "translated by machine" notice and error reports
adapters/extension/        developer-mode browser extension for demos
orchestrator/
  api/                     the HTTP API (FastAPI), rate limits
  pipeline/                masking, segments, tags, glossary   (guarded: see below)
  service/                 translate service; model_call.py sends text to the model
  store/                   PostgreSQL, Redis cache, translation memory
  queue/                   background jobs and the worker
  upstream/                the GovTech API client, quota, mock translator
  governance/              sites, tiers, audit trail
  locale/dz.py             everything Dzongkha-specific on the server
tests/                     Python tests; tests/fixtures/ holds shared examples
tools/                     check.py, demo.py, quality gates, probes
deploy/vm/                 how it runs on the GovTech VM
docs/                      requirements, spec, backlog, design records
```

Good first files to read, small and central:

1. `tests/fixtures/extraction/cases.json`: real examples of HTML in, segments out
2. `adapters/widget/src/extract.ts`
3. `orchestrator/pipeline/protect.py`: how numbers and links are protected
4. `orchestrator/service/model_call.py`: how text is sent to the model

## 6. How we work

- **Every change has tests.** A bug fix starts with a test that fails without
  the fix. Name tests after the requirement they check:
  `test_fr140_…`, `it("fr210_…")`.
- **`python tools/check.py` passes before you ask for review.**
- **Work on your own branch**, never directly on `main` or the team branch. Open
  a pull request; your mentor reviews it.
- **Commit messages cite requirement IDs**, e.g.
  `fix: keep phone numbers with extensions intact (FR-140)`. Only cite IDs that
  exist in `docs/00-requirements.md`.
- **`orchestrator/pipeline/` is guarded:** changes there are checked by
  `tools/check_pipeline_guard.py` and need extra care in review, because a
  mistake there can alter numbers in a government page.
- **Dzongkha-specific code lives in two files only:** `orchestrator/locale/dz.py`
  and `adapters/widget/src/locale-dz.ts`. A check enforces it.
- **Ask early.** An hour stuck is a signal to ask, not to push on.

## 7. Your first week

| Day | Goal |
|---|---|
| 1 | Read sections 1-3 and `docs/CLAUDE.md`. Clone and install. |
| 2 | Run `tools/demo.py` and `tools/check.py`. Skim `docs/00-requirements.md`; list ten IDs you saw in the code. |
| 3 | Read `cases.json` and `extract.ts`. Add one new extraction case to `cases.json` (an HTML shape you think is tricky) and see both test suites run it. |
| 4 | Read `protect.py` and its tests. Find one kind of number or ID a government page might show that you think is not protected; write a test for it. |
| 5 | Pick a first task with your mentor (below). Explain the change you plan before you write it. |

Good first tasks: a new extraction or masking test case from a real-looking
(invented) government sentence; a small, well-defined item from `TODOS.md`;
clearer error messages in a `tools/` script. Each comes with tests.

## 8. Glossary

| Term | Meaning |
|---|---|
| **Dzongkha** (རྫོང་ཁ) | Bhutan's national language, written in Tibetan (Uchen) script |
| **tsheg** `་` | separates syllables (not words: Dzongkha has no spaces between words) |
| **shad** `།` | ends a sentence, like a full stop |
| **DCDD** | Dzongkha Development Commission: the language experts who approve translations |
| **NMT** | neural machine translation: GovTech's model |
| **WSO2** | GovTech's API gateway, in front of the translation model |
| **widget** | the script in the reader's browser that adds the button and swaps text |
| **orchestrator** | the dzweb server (API + worker) |
| **segment** | one block of text sent for translation (a paragraph, a heading, a button label) |
| **slot** | a stretch of text inside a segment, between inline tags such as links |
| **wire format** | how a segment travels: `⟦1⟧…⟦/1⟧` for inline tags, `⟦CUR:1⟧` for a protected amount |
| **masking / entities** | replacing numbers, dates, IDs, emails and links with placeholders before the model, and restoring them after |
| **Tier 1 / 2 / 3** | how strictly content is handled: 1 = legal and fees, never machine-translated; 2 = normal pages; 3 = low-risk |
| **TM** | translation memory: stored translations, approved or machine |
| **fail open** | on any error, show English rather than something broken |
| **chrF++** | the automatic translation-quality score our quality gate uses |
| **FR / NFR** | functional / non-functional requirement IDs in `docs/00-requirements.md` |

## 9. What you will not get, and why

- **GovTech API credentials and the VM's passwords.** You do not need them to
  develop; the mock translator covers it. They grant access to government
  systems.
- **The frozen evaluation set (`eval/frozen`) and the G2C portal snapshot.**
  They hold real government text and officials' contact details, and are kept
  out of the repository on purpose. If a task needs them, your mentor arranges
  it.
