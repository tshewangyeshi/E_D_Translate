"""Requirement-ID check (backlog S0.3, ER-O8).

Every FR-xxx / NFR-xxx cited in code or tests, and every ``test_frNNN`` /
``test_nfrNNN`` test name, must be defined in docs/00-requirements.md with
status R or P. Range-only rows (status ``?``) cannot be cited until the SRS
owner defines them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "docs" / "00-requirements.md"
SCAN_DIRS = ["orchestrator", "tests", "adapters/widget/src", "adapters/widget/test", "tools"]
SCAN_SUFFIXES = {".py", ".ts", ".json"}

_ROW = re.compile(r"^\|\s*((?:N?FR)-\d{3})(?:\s*…\s*((?:N?FR)-\d{3}))?\s*\|.*\|\s*([RP?])\s*\|\s*$")
_CITE = re.compile(r"\b(N?FR)-(\d{3})\b")
_TEST_NAME = re.compile(r"\btest_(n?fr)(\d{3})_")


def load_requirements(path: Path = REQUIREMENTS) -> dict[str, str]:
    """Map ID -> status ('R', 'P' or '?')."""
    status: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        first, last, state = m.group(1), m.group(2), m.group(3)
        if last:
            prefix, lo = first.rsplit("-", 1)
            hi = last.rsplit("-", 1)[1]
            for n in range(int(lo), int(hi) + 1):
                status[f"{prefix}-{n:03d}"] = state
        else:
            status[first] = state
    return status


def iter_citations(root: Path = ROOT) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in SCAN_SUFFIXES or "node_modules" in path.parts:
                continue
            if path.name == "check_req_ids.py":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in _CITE.finditer(line):
                    found.append((path, lineno, f"{m.group(1)}-{m.group(2)}"))
                for m in _TEST_NAME.finditer(line):
                    found.append((path, lineno, f"{m.group(1).upper()}-{m.group(2)}"))
    return found


def main() -> int:
    status = load_requirements()
    if not status:
        print(f"check_req_ids: no requirement rows parsed from {REQUIREMENTS}")
        return 1
    errors = []
    for path, lineno, rid in iter_citations():
        state = status.get(rid)
        where = f"{path.relative_to(ROOT)}:{lineno}"
        if state is None:
            errors.append(f"{where}: {rid} is not defined in docs/00-requirements.md")
        elif state == "?":
            errors.append(f"{where}: {rid} is range-only ('?'); SRS owner must define it first")
    for e in errors:
        print(e)
    print(f"check_req_ids: {len(status)} IDs known, {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
