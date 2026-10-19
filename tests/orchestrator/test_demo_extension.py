"""The developer-mode demo extension (adapters/extension) names one ID everywhere.

Chrome derives the ID from the manifest key; the API enrols it (demo sites.json)
and the header rule sets it as the Origin. If any of the three drifts, the demo
API answers 403 and the portal silently stays English.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from orchestrator.governance.sites import SiteRegistry

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "adapters" / "extension"


def extension_id() -> str:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def test_the_demo_api_enrols_the_extension() -> None:
    origin = f"chrome-extension://{extension_id()}"
    assert SiteRegistry.load(ROOT / "tools" / "demo_site" / "sites.json").allows("portal", origin)


def test_the_header_rule_sets_the_extension_origin_on_api_calls_only() -> None:
    (rule,) = json.loads((EXTENSION / "rules.json").read_text(encoding="utf-8"))
    (header,) = rule["action"]["requestHeaders"]
    assert header == {
        "header": "Origin",
        "operation": "set",
        "value": f"chrome-extension://{extension_id()}",
    }
    assert rule["condition"]["urlFilter"] == "|http://127.0.0.1:8000/"
    assert rule["condition"]["initiatorDomains"] == [extension_id()]


def test_the_extension_reaches_only_the_local_api_and_runs_only_on_the_portal() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["host_permissions"] == ["http://127.0.0.1:8000/*"]
    assert [c["matches"] for c in manifest["content_scripts"]] == [["https://g2c.tech.gov.bt/*"]]
    assert 'const API = "http://127.0.0.1:8000/"' in (EXTENSION / "background.js").read_text(
        encoding="utf-8"
    )
