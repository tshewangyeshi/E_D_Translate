"""Build the widget's self-hosted Dzongkha font (S6.1, FR-340).

    curl -L -o NotoSerifTibetan.ttf \\
      "https://github.com/google/fonts/raw/main/ofl/notoseriftibetan/NotoSerifTibetan%5Bwght%5D.ttf"
    curl -L -o OFL.txt https://github.com/google/fonts/raw/main/ofl/notoseriftibetan/OFL.txt
    python tools/build_dz_font.py NotoSerifTibetan.ttf OFL.txt

Noto Serif Tibetan (SIL Open Font License 1.1) is the default until the DDC
Uchen web-embedding licence is confirmed (spec section 2.7, open question 5);
GovTech is to confirm the OFL fits its policy. The output keeps every layout
feature -- stacked syllables are built by them -- at Regular weight only, so
the file stays small enough for a phone on mobile data. Only Tibetan is kept: the widget
declares the face with a Tibetan unicode-range, so Latin text in a translated
block keeps an ordinary system font and the file is fetched only when
Dzongkha is on the page.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "adapters" / "widget" / "fonts"
NAME = "dzweb-dzongkha.woff2"

#: Tibetan, the zero-width characters the widget inserts, and the dotted
#: circle shapers draw for a mark with no base.
UNICODES = [*range(0x0F00, 0x1000), 0x200B, 0x200C, 0x200D, 0x25CC]


def build(source: Path, licence: Path) -> Path:
    from fontTools import subset  # type: ignore[import-untyped]

    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]  # stacking and positioning live here
    options.name_IDs = ["*"]  # keep the copyright and licence names (OFL)
    options.notdef_outline = True
    font = subset.load_font(str(source), options)
    # Regular only: the weight axis more than doubled the file (718 KB with it),
    # and a browser emboldens a heading itself when no bold face is declared.
    from fontTools.varLib import instancer  # type: ignore[import-untyped]

    font = instancer.instantiateVariableFont(font, {"wght": 400})
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=UNICODES)
    subsetter.subset(font)
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / NAME
    subset.save_font(font, str(target), options)
    shutil.copyfile(licence, OUT / "OFL.txt")
    return target


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    target = build(Path(argv[1]), Path(argv[2]))
    print(f"{target.name}: {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
