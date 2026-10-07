"""The development page server never serves a file outside its folders.

Found by /cso on 2026-10-05: the server joined the request path onto its
folder unchecked, so a raw ``/../../.env`` read the repository's .env with the
GovTech client secret. Development only, bound to 127.0.0.1, but the planned
GovTech VM is shared.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SERVE = Path(__file__).resolve().parents[2] / "tools" / "demo_site" / "serve.py"
_spec = importlib.util.spec_from_file_location("demo_serve", SERVE)
assert _spec and _spec.loader
serve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve)


@pytest.mark.parametrize(
    "path",
    [
        "/../../.env",
        "/%2e%2e/%2e%2e/.env",
        "/g2c/../../../.env",
        "/widget/../../../../.env",
        "/g2c/%2E%2E/%2E%2E/.env",
        "/..%2f..%2f.env",
    ],
)
def test_a_path_leaving_its_folder_is_not_served(path: str) -> None:
    assert serve.resolve(path) == serve.NOWHERE
    assert not serve.NOWHERE.exists()


def test_a_drive_letter_never_reaches_outside_the_folders() -> None:
    """On Windows "/C:/..." would name another drive; on Linux it is a folder name.
    Either way the file served must lie inside a folder this server serves."""
    target = serve.resolve("/C:/Windows/win.ini")
    roots = (serve.HERE, serve.G2C, serve.WIDGET)
    assert target == serve.NOWHERE or any(target.is_relative_to(r.resolve()) for r in roots)


@pytest.mark.parametrize(
    ("path", "root", "name"),
    [
        ("/", "HERE", "index.html"),
        ("/index.html?x=1", "HERE", "index.html"),
        ("/g2c/", "G2C", "index.html"),
        ("/g2c/data/services.json", "G2C", "services.json"),
        ("/widget/main.js", "WIDGET", "main.js"),
    ],
)
def test_the_demo_pages_are_still_served(path: str, root: str, name: str) -> None:
    target = serve.resolve(path)
    assert target.name == name
    assert target.is_relative_to(getattr(serve, root).resolve())
