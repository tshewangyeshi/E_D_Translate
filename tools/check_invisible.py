"""Invisible-character check: source and docs must not contain characters a reader cannot see.

Zero-width spaces are real data in this project (FR-160 inserts them at render
time), so they must always be written as visible escapes (``"\\u200b"``), never
as literal characters. Also rejects NUL and other C0 control characters,
which made tools treat a spec file as binary once already.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".ts", ".js", ".json", ".md", ".toml", ".yml", ".yaml", ".css", ".html"}
NAMES = {"Makefile"}
SKIP_PARTS = {"node_modules", ".venv", ".git", "__pycache__", "dist", ".mypy_cache", ".ruff_cache"}

INVISIBLE = {
    0x200B: "ZERO WIDTH SPACE",
    0x200C: "ZERO WIDTH NON-JOINER",
    0x200D: "ZERO WIDTH JOINER",
    0x2060: "WORD JOINER",
    0xFEFF: "BYTE ORDER MARK / ZWNBSP",
    0x00AD: "SOFT HYPHEN",
}


def _problem(ch: str) -> str | None:
    cp = ord(ch)
    if cp in INVISIBLE:
        return INVISIBLE[cp]
    if cp < 0x20 and ch not in "\t\n\r":
        return f"control character U+{cp:04X}"
    return None


def violations(root: Path = ROOT) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP_PARTS & set(path.relative_to(root).parts):
            continue
        if path.suffix not in SUFFIXES and path.name not in NAMES:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for col, ch in enumerate(line, 1):
                if why := _problem(ch):
                    out.append(f"{rel}:{lineno}:{col}: {why} (write it as an escape)")
    return out


def main() -> int:
    problems = violations()
    for p in problems:
        print(p)
    print(f"check_invisible: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
