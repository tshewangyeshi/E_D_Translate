"""`make check`: lint, type check, both unit suites, the gate suites and repo checks.

Runs the same way on Windows (``python tools/check.py``) and in CI (``make check``).
Every step runs even if an earlier one fails, and the exit code is non-zero if
any step failed. Steps that cannot run yet say so explicitly: nothing is
silently skipped.
"""

from __future__ import annotations

import shutil
import socket
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
    ("invisible characters", [PY, "tools/check_invisible.py"], ROOT),
    ("pipeline change guard", [PY, "tools/check_pipeline_guard.py"], ROOT),
    ("pytest (orchestrator)", [PY, "-m", "pytest", "--ignore=tests/orchestrator/gates"], ROOT),
    (
        "entity-preservation gate, mock (NFR-201)",
        [PY, "-m", "pytest", "tests/orchestrator/gates/test_entity_gate.py"],
        ROOT,
    ),
    (
        "masker recall, held-out (FR-140)",
        [
            PY,
            "tools/masker_recall.py",
            "tests/fixtures/masking/labelled-synthetic.json",
            "--split",
            "heldout",
        ],
        ROOT,
    ),
]

NOT_YET: list[tuple[str, str]] = [
    ("entity gate on real model output (NFR-201)", "needs WSO2 recordings (S0.1) and S10.2"),
    ("tag-integrity gate (NFR-200)", "arrives with S1.5 restoration, after the Sprint 0 go/no-go"),
    ("widget size budget (FR-201)", "arrives with the S4.1 widget build"),
]


INTEGRATION_PORTS = {"PostgreSQL": 55432, "Redis": 56379}  # docker-compose.yml


def services_up() -> dict[str, bool]:
    up = {}
    for name, port in INTEGRATION_PORTS.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                up[name] = True
        except OSError:
            up[name] = False
    return up


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
    require_integration = "--require-integration" in sys.argv[1:]
    steps = list(STEPS)
    up = services_up()
    if all(up.values()):
        steps.append(
            (
                "integration: PostgreSQL + Redis",
                [PY, "-m", "pytest", "-m", "integration"],
                ROOT,
            )
        )
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
    missing = [n for n, ok in up.items() if not ok]
    if missing:
        print(
            f"  NOT RUN  integration tests: {', '.join(missing)} not reachable "
            "(start Docker, then: docker compose up -d)"
        )
    failed = [n for n, ok in results if not ok]
    if missing and require_integration:
        failed.append("integration services unavailable (--require-integration)")
    print(f"\n{'FAILED: ' + ', '.join(failed) if failed else 'All present checks passed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
