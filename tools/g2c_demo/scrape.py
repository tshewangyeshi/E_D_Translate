"""Snapshot the G2C portal's service listings for local translation testing.

    python tools/g2c_demo/scrape.py

The pilot page (https://g2c.tech.gov.bt/g2cportal/ListOfLifeEventComponent) is
an Angular app that fetches its content from ``g2cPortalApi``. This saves the
same JSON to ``tools/g2c_demo/data/`` with the service documents cleaned of
anything executable. The snapshot is git-ignored: the documents name officials
and carry their emails and mobile numbers, and this repository is public.
"""

from __future__ import annotations

import json
import subprocess
import sys
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

API = "https://www.citizenservices.gov.bt/g2cPortalApi"
OUT = Path(__file__).resolve().parent / "data"

#: Formatting the documents use; anything else is dropped, its text kept.
ALLOWED = {
    "a", "b", "blockquote", "br", "div", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "li", "ol", "p", "span", "strong", "sub", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "u", "ul",
}  # fmt: skip
VOID = {"br", "hr"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template"}
ATTRIBUTES = {"href", "style", "color", "face", "size", "colspan", "rowspan", "dir", "align"}


class _Clean(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skipping += 1
        if self.skipping or tag not in ALLOWED:
            return
        kept = []
        for name, value in attrs:
            value = value or ""
            if name not in ATTRIBUTES or "javascript:" in value.lower().replace(" ", ""):
                continue
            if name == "style" and ("url(" in value.lower() or "expression" in value.lower()):
                continue
            kept.append(f' {name}="{escape(value)}"')
        if tag == "a":
            kept.append(' target="_blank" rel="noopener noreferrer"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skipping = max(0, self.skipping - 1)
            return
        if not self.skipping and tag in ALLOWED and tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.out.append(escape(data, quote=False))


def clean(html: str | None) -> str:
    parser = _Clean()
    parser.feed(html or "")
    parser.close()
    return "".join(parser.out)


def _get(path: str) -> list[dict[str, Any]]:
    """Fetch with the system curl: the portal omits an intermediate certificate,
    which Windows' certificate store supplies and Python's bundle does not."""
    done = subprocess.run(  # noqa: S603 - fixed arguments, our own URL
        ["curl", "-sSf", "-m", "60", "-A", "dzweb-local-test", f"{API}/{path}"],  # noqa: S607 - the system curl, on purpose
        capture_output=True,
        check=True,
    )
    data: list[dict[str, Any]] = json.loads(done.stdout)
    return data


def main() -> int:
    categories = _get("getCategory")
    services = _get("getService")
    OUT.mkdir(exist_ok=True)
    (OUT / "categories.json").write_text(
        json.dumps(
            [{k: c[k] for k in ("id", "categoryName", "categoryDescription")} for c in categories],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (OUT / "services.json").write_text(
        json.dumps(
            [
                {
                    "id": s["id"],
                    "category": str(s["category"]),
                    "serviceName": s["serviceName"],
                    "serviceDescription": s["serviceDescription"],
                    "serviceLink": s["serviceLink"],
                    "serviceDocument": clean(s["serviceDocument"]),
                }
                for s in services
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"{len(categories)} categories, {len(services)} services -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
