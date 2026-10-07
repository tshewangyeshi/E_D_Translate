"""Smoke test for dzweb on the VM. Run it any time:

    cd ~/dzweb && set -a && . deploy/vm/dzweb.env && . ./.env.infra && set +a \\
      && .venv/bin/python deploy/vm/smoke.py

Checks the running services and the whole translation path, and prints what
it saw. Exit 0 when everything is right.

1. The API (dzweb-api.service) is up and healthy; its operator routes stay
   hidden without the token.
2. A page request through the API gets HTTP 200. A sentence seen by only one
   visitor is "pending": text one citizen alone was shown is never sent to
   the model or stored (NFR-304). Every request from this VM is one visitor.
3. The full pipeline -- masking, GovTech, storage, cache -- translates a fee
   sentence, as three different visitors would cause, and the amount comes
   back unchanged; a second ask is served from the cache, not the model.

It translates one sentence through GovTech staging per run.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000"
ORIGIN = "https://g2c.tech.gov.bt"
ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"  {'PASS' if passed else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))


def http(
    path: str, body: dict[str, object] | None = None, token: str | None = None
) -> tuple[int, str]:
    method = "POST" if body is not None else "GET"
    request = urllib.request.Request(API + path, method=method)  # noqa: S310 - our own http API
    if body is not None:
        request.data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", ORIGIN)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - our own API
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def main() -> int:
    from orchestrator.service.translate import SegmentIn
    from orchestrator.wiring import Settings, build, environment

    env = environment(ROOT / ".env")
    token = env.get("DZWEB_OPS_TOKEN", "")

    print("1. the running API")
    status, text = http("/v1/health", token=token)
    health = json.loads(text) if status == 200 else {}
    check("healthy", health.get("status") == "ok", f"HTTP {status}")
    for part in ("postgres", "redis", "queue"):
        check(f"{part} ok", health.get("upstreams", {}).get(part, {}).get("state") == "ok")
    check("hidden without the token", http("/v1/health")[0] == 404)

    print("2. a page request through the API")
    sentence = f"Smoke test {int(time.time())}: apply online for a passport."
    status, text = http(
        "/v1/translate",
        {"site": "g2c", "path": "/g2cportal/smoke", "segments": [{"id": "s0", "text": sentence}]},
    )
    segments = json.loads(text).get("segments", []) if status == 200 else []
    first = segments[0] if segments else {}
    check("HTTP 200", status == 200)
    check(
        "one visitor's text is not sent or stored (pending)",
        first.get("status") == "pending_mt" and first.get("text") == sentence,
        str(first.get("status")),
    )

    print("3. the whole pipeline, as three visitors")
    c = build(Settings.from_env(env), apply_migrations=False)
    site = c.sites.get("g2c")
    assert site is not None
    fee = f"The renewal fee of Nu. 1,500 is paid online (check {int(time.time())})."
    calls_before = c.translator.calls

    async def ask(visitor: str) -> object:
        (out,) = await c.service.translate(
            site, visitor, [SegmentIn("s0", fee)], "/g2cportal/smoke"
        )
        return out

    results = [asyncio.run(ask(f"2026-10-07:smoke-visitor-{n}")) for n in range(3)]
    last = results[-1]
    check("translated on the third visitor", last.status.value == "translated", last.status.value)  # type: ignore[attr-defined]
    check("in Dzongkha", any("ཀ" <= ch <= "ཬ" for ch in last.text))  # type: ignore[attr-defined]
    check("the amount kept its value", "1500" in last.text.replace(",", "") or "༡༥༠༠" in last.text)  # type: ignore[attr-defined]
    print(f"        {last.text}")  # type: ignore[attr-defined]
    calls_after = c.translator.calls
    again = asyncio.run(ask("2026-10-07:smoke-visitor-4"))
    check(
        "served again from storage, not the model",
        c.translator.calls == calls_after and again.status.value == "translated",
    )  # type: ignore[attr-defined]
    check("GovTech was called", calls_after > calls_before)
    c.conn.close()
    c.ops_conn.close()

    print("ALL PASS" if ok else "SOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
