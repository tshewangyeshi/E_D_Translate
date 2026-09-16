# TODOS

## Review

### Codex outside review of the reconciled plan

**What:** Run `/codex review` on `docs/02-technical-spec.md` and `docs/03-backlog.md` once the eng-review remedies are applied.

**Why:** The 2026-09-16 eng review's outside voice was a Claude subagent on the same harness, not an independent model family. A second model reading the plan is a stronger check on the placeholder, tier and caching design.

**Context:** Codex CLI is installed and authenticated on the dev machine. It was deliberately skipped on 2026-09-16 to keep GovTech design docs off third-party AI services. The GitHub repo has since been made public, which may change that policy question; confirm with GovTech before running. Review record: `docs/designs/dzweb-eng-review.md`.

**Effort:** S
**Priority:** P3
**Depends on:** T3 (remedies applied to spec and backlog); GovTech confirmation that external AI review is allowed

## Completed
