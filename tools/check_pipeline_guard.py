"""Pipeline change guard (replaces /guard's edit boundary, which is broken on Windows).

docs/CLAUDE.md: orchestrator/pipeline/ holds entity masking and tag
restoration; an accidental drive-by edit there is a citizen-facing defect.
Rule: a branch that changes files under orchestrator/pipeline/ must also
change tests under tests/orchestrator/, and those tests must cite a
requirement ID (checked separately by check_req_ids.py).

Compares the working tree with the merge base of the base branch
(default origin/main, falling back to main).
"""

from __future__ import annotations

import subprocess
import sys

PIPELINE = "orchestrator/pipeline/"
TESTS = "tests/orchestrator/"


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", *args], capture_output=True, text=True, check=True  # noqa: S607
    ).stdout


def changed_files(base: str) -> set[str]:
    merge_base = _git("merge-base", base, "HEAD").strip()
    tracked = _git("diff", "--name-only", merge_base).split()
    untracked = _git("ls-files", "--others", "--exclude-standard").split()
    return set(tracked) | set(untracked)


def main() -> int:
    for base in ("origin/main", "main"):
        try:
            files = changed_files(base)
            break
        except subprocess.CalledProcessError:
            continue
    else:
        print("check_pipeline_guard: no base branch found; skipped")
        return 0
    pipeline = sorted(f for f in files if f.startswith(PIPELINE))
    tests = [f for f in files if f.startswith(TESTS)]
    if pipeline and not tests:
        print(f"check_pipeline_guard: {PIPELINE} changed without {TESTS} changes:")
        for f in pipeline:
            print(f"  {f}")
        return 1
    print(
        f"check_pipeline_guard: {len(pipeline)} pipeline file(s) changed, "
        f"{len(tests)} test file(s) changed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
