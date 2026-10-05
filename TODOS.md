# TODOS

## Translation quality

### FR-144 sign-off

**What:** Get the SRS owner's sign-off on FR-144 (the model translates amounts, dates, percentages and counts; values checked).

**Why:** It narrows FR-140 and FR-142, and it is the default (`DZWEB_NUMBERS=model`). Until signed, `protected` is the conservative setting.

**Context:** Product owner decision 2026-10-05 after 9 of 9 staging sentences kept every value. Row in `docs/00-requirements.md`.

**Effort:** S
**Priority:** P1
**Depends on:** SRS owner

### Native-reader review of the Dzongkha

**What:** Have Dzongkha readers (DDC or GovTech linguists) score a sample of translations, e.g. 50 segments from `tools/g2c_demo`, for meaning and fluency.

**Why:** Every measurement so far counts what passes our checks, not what reads well. Piece-by-piece translation keeps English order around links, which may read badly in Dzongkha.

**Also:** the widget's own labels (`adapters/widget/test/labels.test.ts`). The toggle was misspelled "ryong kha" until 2026-10-05; the notice, report and pick labels were then rewritten by a non-native writer.

**Effort:** M (mostly other people's time)
**Priority:** P1
**Depends on:** reviewers

### Real glossary

**What:** Load the DCDD termbase and measure glossary-term survival (design note option 3).

**Why:** Glossary placeholders were the main S0.1 failure; the demo runs with an empty termbase.

**Effort:** M
**Priority:** P1
**Depends on:** DCDD termbase

### Dzongkha font on the pilot portal

**What:** Ask GovTech to give `[lang|="dz"]` text a Dzongkha font on g2c.tech.gov.bt.

**Why:** Its service documents pin Times New Roman, which has no Tibetan; translated text falls back to the very small Microsoft Himalaya on Windows.

**Effort:** S
**Priority:** P2
**Depends on:** GovTech

## Review

### Codex outside review of the reconciled plan

**What:** Run `/codex review` on `docs/02-technical-spec.md` and `docs/03-backlog.md` once the eng-review remedies are applied.

**Why:** The 2026-09-16 eng review's outside voice was a Claude subagent on the same harness, not an independent model family. A second model reading the plan is a stronger check on the placeholder, tier and caching design.

**Context:** Codex CLI is installed and authenticated on the dev machine. It was deliberately skipped on 2026-09-16 to keep GovTech design docs off third-party AI services. The GitHub repo has since been made public, which may change that policy question; confirm with GovTech before running. Review record: `docs/designs/dzweb-eng-review.md`.

**Effort:** S
**Priority:** P3
**Depends on:** T3 (remedies applied to spec and backlog); GovTech confirmation that external AI review is allowed

## Tooling

### gstack `/freeze` and `/guard` edit boundary fails on Windows

**What:** Report (or patch upstream) the gstack freeze hook so it recognises Windows drive paths.

**Why:** `~/.claude/skills/gstack/freeze/bin/check-freeze.sh` treats any path not starting with `/` as relative and prefixes the working directory, so `C:\...` paths become `/c/.../C:\...` and **every** edit is denied, even inside the boundary. The repo rule "`/guard` is on for `orchestrator/pipeline/`" therefore can't be followed on this machine.

**Context:** Found 2026-09-18 while starting S1.2. Interim control: `tools/check_pipeline_guard.py` in `make check` fails any branch that changes `orchestrator/pipeline/` without changing `tests/orchestrator/` (documented in `docs/CLAUDE.md`). Fix upstream in gstack (e.g. normalise `^[A-Za-z]:[\\/]` paths with `cygpath -u` before the prefix check), then restore `/guard` as the primary control.

**Effort:** S
**Priority:** P2
**Depends on:** None

## Before the pilot

### Client-hash salt is per process

**What:** Make every API process derive the same daily salt for the client hash, so the distinct-clients count means distinct citizens whichever replica answers.

**Why:** `ClientHasher` (`orchestrator/api/app.py`) draws a random salt in memory. One citizen served by three replicas, or by one process restarted three times in a day, hashes three ways and is counted as three clients, which meets the NFR-304 threshold and sends their page to the model. With a single long-lived process the rule holds; the pilot will not run that way.

**Context:** Found 2026-09-29 while writing the S3.4 tests. `test_nfr304_one_citizen_served_by_several_processes_is_one_client` is a strict `xfail`: it starts failing the build the moment the gap is closed, so the marker cannot be forgotten. Two ways to share a salt, and the choice is a security decision rather than a coding one: (a) keep the day's salt in Redis with an expiry, which puts it next to the hashes it protects; (b) derive it as `HMAC(deployment secret, day)`, which keeps it out of Redis but adds a secret to manage. Either way the counter is already scoped to one UTC day, so nothing outlives the salt. Take it to `/cso`.

**Effort:** S
**Priority:** P1
**Depends on:** A decision on (a) or (b)

### Confirm the review cap number with the SRS owner

**What:** Confirm 500 request-raised review items per site per rolling day, and who runs `python -m orchestrator.ops.review_owed` and how often.

**Why:** The behaviour past the cap was decided on 2026-09-29: the page is served and the item is opened as `owed`, out of the reviewers' queue until released. The number itself is a guess, and owed items only reach reviewers if someone releases them.

**Context:** `StoreSettings.review_items_per_site_per_day`; `dzweb_review_owed` shows the backlog.

**Effort:** S
**Priority:** P2
**Depends on:** SRS owner, DCDD reviewer capacity

### Separate the migration owner role from the runtime role

**What:** Run migrations as a role the service never uses, and run the API and worker as a role with INSERT and SELECT only on `audit_event`.

**Why:** The append-only triggers refuse UPDATE, DELETE and TRUNCATE, but the table's owner can switch them off, rewrite a row and switch them back without trace. Today the service migrates with its own role, so it owns the table (found by the 2026-09-29 review).

**Context:** `orchestrator/store/migrate.py` runs from `wiring.build()`. The change is a deployment and wiring change: a `DZWEB_MIGRATE_DSN`, a migrate step before the processes start, and grants in a migration. Take it to `/cso`.

**Effort:** M
**Priority:** P1
**Depends on:** Deployment topology

### Dzongkha wording for the personal-details hint

**What:** Get DCDD to supply the Dzongkha for "Do not include names, ID numbers or contact details." and add it beside the English in `adapters/widget/src/locale-dz.ts`.

**Why:** The report form's hint (NFR-303) is English only. A developer must not guess Dzongkha wording.

**Effort:** S
**Priority:** P2
**Depends on:** DCDD

### Worker metrics are not exported

**What:** Export the background worker's counts (stored, invalid by cause, upstream retries, swept leases) the way the API exports its own.

**Why:** `/v1/metrics` describes the live request path of the process that answers it. Most translation happens in the worker, which is a separate process with no HTTP surface, so its outcomes are visible only in the summary line it logs per batch (`worker claimed=… swept=… stored=… invalid:<cause>=…`).

**Context:** `WorkerReport` in `orchestrator/queue/worker.py` already has the numbers per iteration. Options: a small metrics listener in the worker process, or periodic writes to a table the API reads. Decide with the operations dashboard (S9.3).

**Effort:** S
**Priority:** P2
**Depends on:** None

### PostgreSQL connection pool

**What:** Replace the single connection per process in `orchestrator/wiring.py` with a `psycopg_pool` pool, and run store calls off the event loop (or switch to psycopg's async API).

**Why:** Correct today but serialised: every request in an API process shares one connection and blocks the event loop during database I/O. Fine for tests, not for pilot traffic.

**Context:** The TM, queue and seen-counter already sit behind interfaces, so this is contained to wiring plus the Postgres adapters. NFR-100 (p95 < 300 ms cached) should be re-measured against real PostgreSQL afterwards.

**Effort:** M
**Priority:** P1
**Depends on:** None

### Trusted-proxy client addresses

**What:** Read the client address from `X-Forwarded-For` only when the direct peer is a configured trusted proxy (WSO2 / load balancer).

**Why:** Behind a proxy every citizen shares the proxy's address, which breaks both the N-distinct-clients rule (NFR-304: nothing would ever be translated) and the per-client rate limit (everyone throttled together).

**Context:** `orchestrator/api/app.py` uses `request.client.host`. Needs the deployment topology (FR-600) to know which proxies to trust. Must be tested with spoofed headers from untrusted peers.

The 2026-09-28 security audit closed the two limiter defects behind this one (LRU eviction so a penalty survives memory pressure; IPv6 folded to its /64 so a new bucket costs an allocation rather than an address). Neither helps while every request carries the proxy's address, so this remains the blocking item: it decides what the client key means before any limiting applies. Whatever resolves the address must feed `client_bucket` so the /64 rule applies to the real peer.

**Effort:** S
**Priority:** P1
**Depends on:** WSO2/deployment topology

### Chunk widget extraction at block boundaries

**What:** Split `extract()` so a long page is walked in pieces with the main thread handed back between them, instead of one synchronous pass.

**Why:** ER-20 budgets no widget long task over 50 ms. Measured on a 600-block page with the CPU throttled 6x, the worst task is ~77-93 ms and a single `extract()` over the whole page accounts for ~68 ms of it. Yielding between writes cannot help: the cost is paid before the first write. On the low-end Android hardware this service targets, that window is felt as a dead tap.

**Context:** `adapters/widget/test/e2e/perf.spec.ts` measures it and currently asserts regression guards (130 ms / 160 ms) that sit ABOVE the ER-20 targets, with the gap written into the test. Tighten them to 50 ms and 100 ms when this lands. The work is in `adapters/widget/src/extract.ts`: chunk roots must stop descending at block boundaries, or an inline element becomes its own block and the placeholder model breaks.

**Effort:** M
**Priority:** P2 (before the pilot on low-end devices; not before a desktop demo)
**Depends on:** None

## Completed
