# TODOS

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

**Effort:** S
**Priority:** P1
**Depends on:** WSO2/deployment topology

## Completed
