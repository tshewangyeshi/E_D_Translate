"""Assemble the developer-mode demo extension (development only).

    cd adapters/widget && npm run build
    python tools/build_extension.py

Writes ``adapters/extension/dist``: the extension's own files plus the built
widget under ``widget/``. Load that folder in chrome://extensions (or
edge://extensions) with "Load unpacked"; see adapters/extension/README.md.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "adapters" / "extension"
WIDGET = ROOT / "adapters" / "widget" / "dist"
OUT = SOURCE / "dist"
OWN_FILES = ("manifest.json", "background.js", "content.js", "rules.json")


def build(out: Path = OUT) -> Path:
    if not (WIDGET / "main.js").exists():
        raise FileNotFoundError("build the widget first: cd adapters/widget && npm run build")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in OWN_FILES:
        shutil.copy2(SOURCE / name, out / name)
    shutil.copytree(WIDGET, out / "widget")
    return out


def main() -> int:
    try:
        out = build()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2
    print(f"extension ready: load {out} with 'Load unpacked'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
