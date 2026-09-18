"""`make check`: lint, type check, both unit suites, the gate suites and repo checks.

Runs the same way on Windows (``python tools/check.py``) and in CI (``make check``).
Every step runs even if an earlier one fails, and the exit code is non-zero if
any step failed. Steps that cannot run yet say so explicitly: nothing is
silently skipped.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIDGET = ROOT / "adapters" / "widget"
PY = sys.executable


def _npx() -> str:
    found = shutil.which("npx") or shutil.which("npx.cmd")
    if not found:
        raise FileNotFoundError("npx not found: install Node.js 20+")
    return found


STEPS: list[tuple[str, list[str], Path]] = [
    ("ruff (lint)", [PY, "-m", "ruff", "check", "orchestrator", "tools", "tests"], ROOT),
    ("mypy (types)", [PY, "-m", "mypy"], ROOT),
    ("requirement IDs (S0.3)", [PY, "tools/check_req_ids.py"], ROOT),
    ("locale isolation (NFR-500)", [PY, "tools/check_locale.py"], ROOT),
    ("pipeline change guard", [PY, "tools/check_pipeline_guard.py"], ROOT),
    ("pytest (orchestrator)", [PY, "-m", "pytest"], ROOT),
]

NOT_YET: list[tuple[str, str]] = [
    ("entity-preservation gate (NFR-201)", "arrives with S1.3 masking and S10.2 gates"),
    ("tag-integrity gate (NFR-200)", "arrives with S1.5 restoration, after the Sprint 0 go/no-go"),
    ("widget size budget (FR-201)", "arrives with the S4.1 widget build"),
]


def run(name: str, cmd: list[str], cwd: Path) -> bool:
    start = time.monotonic()
    print(f"\n=== {name} ===", flush=True)
    try:
        ok = subprocess.run(cmd, cwd=cwd, check=False).returncode == 0  # noqa: S603
    except FileNotFoundError as err:
        print(err)
        ok = False
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} ({time.monotonic() - start:.1f}s)")
    return ok


def main() -> int:
    steps = list(STEPS)
    try:
        npx = _npx()
        steps += [
            ("tsc (widget types)", [npx, "tsc", "--noEmit", "-p", "tsconfig.json"], WIDGET),
            ("vitest (widget)", [npx, "vitest", "run"], WIDGET),
        ]
    except FileNotFoundError as err:
        steps.append(("widget", [str(err)], WIDGET))
    results = [(name, run(name, cmd, cwd)) for name, cmd, cwd in steps]

    print("\n=== summary ===")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    for name, why in NOT_YET:
        print(f"  TODO  {name}: {why}")
    failed = [n for n, ok in results if not ok]
    print(f"\n{'FAILED: ' + ', '.join(failed) if failed else 'All present checks passed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
