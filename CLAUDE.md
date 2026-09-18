# dzweb

Project guidance lives in `docs/`. Read these before any work:

- `docs/00-requirements.md` — numbered requirements (reconstructed draft pending SRS-owner sign-off; only cite IDs listed there, never `?` rows).
- `docs/CLAUDE.md` — non-negotiable invariants, Dzongkha domain facts, stack, testing and repo rules (`/guard` on `orchestrator/pipeline/`, `/cso` before release, never paste real citizen data, telemetry off).
- `docs/02-technical-spec.md` and `docs/03-backlog.md` — plan of record.
- `docs/designs/dzweb-eng-review.md` — engineering-review record; its remedies are applied in the spec and backlog.

Requirement IDs (FR-xxx, NFR-xxx) are the vocabulary of this repo: commit messages, PR bodies and test names cite them.

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python
(cd adapters/widget && npm ci)
docker compose up -d           # throwaway PostgreSQL (55432) + Redis (56379) for integration tests
python tools/check.py          # = make check; CI adds --require-integration
```

Run locally (fake translator; configuration in `orchestrator/wiring.py`):

```bash
export DZWEB_PG_DSN=postgresql://dzweb@127.0.0.1:55432/dzweb_test DZWEB_REDIS_URL=redis://127.0.0.1:56379/0
export DZWEB_TERMBASE=tests/fixtures/glossary/termbase-sample.json DZWEB_SITES=sites.json
export DZWEB_TRANSLATOR=mock DZWEB_ALLOW_MOCK_TRANSLATOR=1   # dev only: output is "DZ:" + English
uvicorn orchestrator.main:create --factory                   # API
python -m orchestrator.queue.run_worker                      # background worker
python -m orchestrator.ops.prewarm --site portal segments.json   # after npm run build && node adapters/widget/scripts/export-segments.mjs
```

- Python code: `orchestrator/` (pipeline in `orchestrator/pipeline/`, Dzongkha specifics only in `orchestrator/locale/dz.py`).
- Widget: `adapters/widget/` (TypeScript, vitest + jsdom). Dzongkha specifics only in `src/locale-dz.ts`.
- Shared extraction fixtures: `tests/fixtures/extraction/cases.json` (run by both vitest and pytest).
