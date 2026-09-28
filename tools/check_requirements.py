"""`requirements.txt` must not drift from `pyproject.toml` or the environment.

`pyproject.toml` declares ranges and is authoritative; `requirements.txt` pins
the closure for tools that cannot read PEP 621 metadata (the security audit
among them). Two files holding versions is two files that can disagree, and a
stale pin file is worse than none: it reports a version nobody is running, so a
CVE check answers about software that is not installed.

Two classes of check, because they are not equally portable:

* **Fatal** -- every line is an exact pin, and every dependency declared in
  `pyproject.toml` is present and satisfies its declared range. This compares
  two files in the repository, so it gives the same answer on every machine.

* **Environment** -- pins against what is actually installed. This is the check
  that catches "I upgraded a package and forgot to regenerate", but it is
  specific to one interpreter on one platform: a fresh install resolving
  `fastapi>=0.115` may legitimately land elsewhere, and `colorama` is a Windows
  dependency. Fatal on a development machine, reported without failing under
  ``CI``, where the environment is built from the ranges rather than the pins.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)$")
REGENERATE = (
    "regenerate with: .venv/Scripts/python -m pip freeze --exclude-editable "
    "| sort > requirements.txt   (then restore the header)"
)


def canonical(name: str) -> str:
    """PEP 503 name normalisation: Types-Redis and types_redis are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(text: str) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    errors: list[str] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.match(line)
        if match is None:
            errors.append(
                f"requirements.txt:{number}: {line!r} is not an exact pin. "
                "Ranges, URLs, editable paths, includes and index options are not allowed "
                "-- the file exists to say what is installed, not what is permitted."
            )
            continue
        name, pinned = canonical(match.group(1)), match.group(2)
        if name in seen:
            errors.append(
                f"requirements.txt:{number}: {name} pinned again (first at line {seen[name]})"
            )
            continue
        seen[name] = number
        pins[name] = pinned
    return pins, errors


def declared_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    return declared


def check_declared(pins: dict[str, str], declared: list[str] | None = None) -> list[str]:
    """Every declared dependency is pinned, and the pin satisfies the declared range."""
    try:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip and pytest
        return ["packaging is not installed: cannot verify pins against pyproject.toml"]

    errors = []
    for raw in declared_dependencies() if declared is None else declared:
        requirement = Requirement(raw)
        name = canonical(requirement.name)
        pinned = pins.get(name)
        if pinned is None:
            errors.append(
                f"{name} is declared in pyproject.toml but missing from requirements.txt "
                f"-- {REGENERATE}"
            )
            continue
        if not requirement.specifier:
            continue
        try:
            ok = requirement.specifier.contains(Version(pinned), prereleases=True)
        except InvalidVersion:
            errors.append(f"{name}: pinned version {pinned!r} is not a valid version")
            continue
        if not ok:
            errors.append(
                f"{name}: pinned {pinned}, which does not satisfy pyproject.toml's "
                f"{requirement.specifier}. The pin and the declaration disagree."
            )
    return errors


def check_environment(pins: dict[str, str]) -> list[str]:
    """Pins against what is importable here. Platform- and resolution-specific."""
    drift = []
    for name, pinned in sorted(pins.items()):
        try:
            actual = installed_version(name)
        except PackageNotFoundError:
            drift.append(f"{name}=={pinned} is pinned but not installed")
            continue
        if canonical(actual) != canonical(pinned):
            drift.append(f"{name}: pinned {pinned}, installed {actual}")
    return drift


def main() -> int:
    if not REQUIREMENTS.exists():
        print(f"FAIL: {REQUIREMENTS.name} is missing -- {REGENERATE}")
        return 1

    pins, errors = read_pins(REQUIREMENTS.read_text(encoding="utf-8"))
    if not pins and not errors:
        print("FAIL: requirements.txt pins nothing")
        return 1
    errors += check_declared(pins)

    for error in errors:
        print(f"FAIL: {error}")

    drift = check_environment(pins)
    if drift:
        # Under CI the environment is built from pyproject's ranges, so a
        # different resolution is expected rather than a mistake.
        fatal = not os.environ.get("CI")
        label = "FAIL" if fatal else "NOTE"
        print(f"\n{label}: requirements.txt does not match the installed environment:")
        for line in drift:
            print(f"  {line}")
        print(f"  {REGENERATE}")
        if not fatal:
            print("  (not fatal under CI: the environment is installed from pyproject ranges)")
        elif not errors:
            return 1

    if errors:
        return 1
    if not drift:
        print(f"requirements.txt: {len(pins)} pins, consistent with pyproject.toml and installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
