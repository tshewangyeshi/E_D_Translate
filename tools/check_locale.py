"""NFR-500 check: Dzongkha-specific code lives only in the two locale homes.

Fails on Tibetan code points (U+0F00-U+0FFF) outside the allowed paths,
whether written literally or as an escape (\\u0Fxx, \\x{0Fxx}, &#x0Fxx;, or
decimal HTML entities 3840-4095). The escape patterns are assembled at runtime
so this file does not match itself.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_PREFIXES = (
    "orchestrator/locale/",
    "adapters/widget/src/locale-dz.ts",
    "tests/",
    "adapters/widget/test/",
    "docs/",
)
SCAN_DIRS = ["orchestrator", "adapters/widget/src", "tools"]
SCAN_SUFFIXES = {".py", ".ts", ".js", ".css", ".html", ".json"}

_B = "\\\\"  # a literal backslash in regex source
_LITERAL = re.compile("[ༀ-࿿]")
_ESCAPES = re.compile(
    _B + "u0[fF][0-9a-fA-F]{2}"  # \u0Fxx
    + "|" + _B + "x\\{0?[fF][0-9a-fA-F]{2}\\}"  # \x{0Fxx}
    + "|&#[xX]0?[fF][0-9a-fA-F]{2};"  # &#x0Fxx;
    + "|&#(?:38[4-9][0-9]|39[0-9]{2}|40[0-8][0-9]|409[0-5]);"  # &#3840; .. &#4095;
)


def violations(root: Path = ROOT) -> list[str]:
    out: list[str] = []
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in SCAN_SUFFIXES or "node_modules" in path.parts:
                continue
            posix = path.relative_to(root).as_posix()
            if posix.startswith(ALLOWED_PREFIXES) or posix == "tools/check_locale.py":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _LITERAL.search(line) or _ESCAPES.search(line):
                    out.append(f"{posix}:{lineno}: Tibetan script outside a locale home (NFR-500)")
    return out


def main() -> int:
    problems = violations()
    for p in problems:
        print(p)
    print(f"check_locale: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
