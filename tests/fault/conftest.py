"""A real deployment to break on purpose (S10.4).

Real PostgreSQL and Redis (docker compose), the real API, worker and queue,
and a GovTech gateway stand-in that the test controls: answers, 503s,
garbage, or silence. The translator is the production ``Wso2Translator``
talking to that stand-in, so the error handling under test is the real one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.testing.rig import OPS, OPS_TOKEN, ORIGIN, TERMBASE
from orchestrator.upstream.wso2 import Wso2Config, Wso2Translator
from orchestrator.wiring import Components, Settings, build, record_configuration
from tests.orchestrator.conftest import pg_conn, redis_client, redis_url  # noqa: F401 - fixtures

PORTAL = {
    "site_id": "portal",
    "origins": [ORIGIN],
    "default_tier": 2,
    "path_rules": [{"path": "/legal/*", "tier": 1}],
}
TOKEN_URL = "https://sso.gateway.test/oauth2/token"  # noqa: S105 - a URL, not a secret
URL = "https://gateway.test/translate"


class Gateway:
    """The GovTech gateway, scripted: ``mode`` is ok, slow, 503 or garbage."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        self.calls += 1
        text = json.loads(request.content)["text"]
        if self.mode == "slow":
            await asyncio.sleep(5)
        if self.mode == "503":
            return httpx.Response(503, text="upstream unavailable")
        if self.mode == "garbage":
            return httpx.Response(200, text="<html>gateway error page</html>")
        return httpx.Response(200, json={"translated_text": f"DZ {text}", "chunks": 1})

    def translator(self) -> Wso2Translator:
        config = Wso2Config(URL, TOKEN_URL, "client", "secret", "fault-model", timeout_seconds=10)
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return Wso2Translator(config, client)


class Deployment:
    def __init__(self, pg: Any, redis: str, tmp_path: Path) -> None:
        schema = pg.execute("SELECT current_schema()").fetchone()[0]
        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps({"sites": [PORTAL]}), encoding="utf-8")
        self.env = {
            "DZWEB_PG_DSN": pg.info.dsn + f" options=-csearch_path={schema}",
            "DZWEB_REDIS_URL": redis,
            "DZWEB_TERMBASE": str(TERMBASE),
            "DZWEB_SITES": str(sites),
            "DZWEB_TRANSLATOR": "mock",
            "DZWEB_ALLOW_MOCK_TRANSLATOR": "1",
            "DZWEB_UPSTREAM_RPS": "1000",
            "DZWEB_DISTINCT_CLIENTS": "1",
            "DZWEB_OPS_TOKEN": OPS_TOKEN,
        }
        self.gateway = Gateway()
        self.started: list[Components] = []

    def start(self, **env: str) -> Components:
        c = build(Settings.from_env({**self.env, **env}))
        record_configuration(c)
        c.service.translator = self.gateway.translator()
        self.started.append(c)
        return c

    def close(self) -> None:
        for c in self.started:
            for conn in (c.conn, c.ops_conn):
                if not conn.closed:
                    conn.close()


@pytest.fixture
def deployment(
    pg_conn: Any,  # noqa: F811 - the fixture, imported above
    redis_client: Any,  # noqa: F811
    redis_url: str,  # noqa: F811
    tmp_path: Path,
) -> Iterator[Deployment]:
    d = Deployment(pg_conn, redis_url, tmp_path)
    yield d
    d.close()


def client(c: Components) -> TestClient:
    app = create_app(
        service=c.service,
        sites=c.sites,
        termbase_version=c.termbase.version,
        health=c.health,
        gauges=c.gauges,
        ops_token=OPS_TOKEN,
    )
    return TestClient(app, client=("203.0.113.1", 50000))


def ask(
    api: TestClient, *texts: str, path: str = "/services/renewal"
) -> tuple[int, list[dict[str, Any]]]:
    response = api.post(
        "/v1/translate",
        json={
            "site": "portal",
            "path": path,
            "segments": [{"id": f"s{n}", "text": t} for n, t in enumerate(texts)],
        },
        headers={"Origin": ORIGIN},
    )
    return response.status_code, response.json().get("segments", [])


def metric(api: TestClient, name: str, match: Callable[[str], bool] = lambda line: True) -> float:
    """The sum of a metric's samples whose line passes ``match`` (0 if none)."""
    text = api.get("/v1/metrics", headers=OPS).text
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name) and match(line):
            total += float(line.rsplit(" ", 1)[1])
    return total
