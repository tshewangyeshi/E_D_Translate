"""The GovTech translation client (Sprint 0). Requirements: FR-100, FR-150, FR-155, NFR-410.

Every test here runs against a fake HTTP layer (httpx.MockTransport) that
answers the way staging answered the probe on 2026-10-05. Nothing in this
file reaches GovTech; the one live test is opt-in and never part of
``make check``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.pipeline.segment import parse
from orchestrator.upstream.errors import (
    UpstreamAuthError,
    UpstreamBadResponse,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from orchestrator.upstream.wso2 import Wso2Config, Wso2Translator
from orchestrator.wiring import DEFAULT_MODEL_VERSION, Settings, build_translator, environment

URL = "https://gateway.example/translate"
TOKEN_URL = "https://sso.example/oauth2/token"  # noqa: S105 - a URL, not a secret
CLIENT_ID = "test-client-id"
SECRET = "test-client-secret-value"  # noqa: S105 - a fixed test value
REF = parse("Apply online")
TRACED = {"x-dsai-request-id": "r"}


class FakeGateway:
    """Token endpoint and translate endpoint, scripted per test."""

    def __init__(self) -> None:
        self.token_requests: list[httpx.Request] = []
        self.translate_requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.expires_in: Any = 3600
        self.translate: Callable[[httpx.Request], httpx.Response] = self.ok
        self.in_flight = 0
        self.max_in_flight = 0

    def ok(self, request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["text"]
        return httpx.Response(
            200,
            json={"translated_text": f"DZ[{text}]", "chunks": 1, "cached": False},
            headers={"x-dsai-request-id": "req-1"},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.token_requests.append(request)
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.tokens_issued}",
                    "token_type": "Bearer",
                    "expires_in": self.expires_in,
                },
            )
        self.translate_requests.append(request)
        return self.translate(request)

    def client(self, token_store: Any = None) -> Wso2Translator:
        config = Wso2Config(URL, TOKEN_URL, CLIENT_ID, SECRET, "model-x", concurrency=2)
        transport = httpx.MockTransport(self.handler)
        return Wso2Translator(config, httpx.AsyncClient(transport=transport), token_store)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the happy path ---


def test_fr100_sends_one_text_and_returns_the_translation() -> None:
    gw = FakeGateway()
    assert run(gw.client().translate("Apply ⟦1⟧online⟦/1⟧", REF)) == "DZ[Apply ⟦1⟧online⟦/1⟧]"
    (request,) = gw.translate_requests
    assert json.loads(request.content) == {"text": "Apply ⟦1⟧online⟦/1⟧"}
    assert request.headers["authorization"] == "Bearer token-1"


def test_fr100_the_token_request_uses_client_credentials() -> None:
    gw = FakeGateway()
    run(gw.client().translate("Apply online", REF))
    (request,) = gw.token_requests
    assert request.content == b"grant_type=client_credentials"
    assert request.headers["authorization"].startswith("Basic ")


def test_fr155_a_token_is_fetched_once_and_reused() -> None:
    gw = FakeGateway()
    client = gw.client()

    async def many() -> None:
        await asyncio.gather(*(client.translate(f"text {n}", REF) for n in range(10)))
        await client.translate("one more", REF)

    run(many())
    assert gw.tokens_issued == 1  # concurrent callers share one fetch
    assert len(gw.translate_requests) == 11


def test_fr155_a_token_is_renewed_before_it_expires() -> None:
    gw = FakeGateway()
    gw.expires_in = 30  # shorter than the renewal margin: renewed on every call
    client = gw.client()

    async def twice() -> None:
        await client.translate("a", REF)
        await client.translate("b", REF)

    run(twice())
    assert gw.tokens_issued == 2


def test_fr155_calls_in_parallel_are_bounded() -> None:
    gw = FakeGateway()

    async def slow(request: httpx.Request) -> httpx.Response:
        gw.in_flight += 1
        gw.max_in_flight = max(gw.max_in_flight, gw.in_flight)
        await asyncio.sleep(0.01)
        gw.in_flight -= 1
        return gw.ok(request)

    gw.translate = slow  # type: ignore[assignment]
    client = gw.client()

    async def burst() -> None:
        await asyncio.gather(*(client.translate(f"t{n}", REF) for n in range(8)))

    run(burst())
    assert gw.max_in_flight == 2


def test_fr100_blank_text_never_reaches_the_api() -> None:
    """The API answers 422 for empty text; there is nothing to translate anyway."""
    gw = FakeGateway()
    assert run(gw.client().translate("   ", REF)) == "   "
    assert gw.translate_requests == [] and gw.token_requests == []


# --- every failure is an UpstreamError, so the page stays English (NFR-410) ---


def test_nfr410_a_refused_token_is_renewed_once_and_the_call_retried() -> None:
    gw = FakeGateway()
    refusals = iter([httpx.Response(401, json={"code": "900901"})])

    def first_refused(request: httpx.Request) -> httpx.Response:
        return next(refusals, None) or gw.ok(request)

    gw.translate = first_refused
    assert run(gw.client().translate("Apply online", REF)) == "DZ[Apply online]"
    assert gw.tokens_issued == 2
    assert [r.headers["authorization"] for r in gw.translate_requests] == [
        "Bearer token-1",
        "Bearer token-2",
    ]


def test_nfr410_a_fresh_token_refused_too_is_an_auth_error() -> None:
    gw = FakeGateway()
    gw.translate = lambda request: httpx.Response(401, json={"code": "900901"})
    with pytest.raises(UpstreamAuthError):
        run(gw.client().translate("Apply online", REF))
    assert len(gw.translate_requests) == 2  # one retry, not a loop


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), UpstreamRateLimited),
        (httpx.Response(500), UpstreamUnavailable),
        (httpx.Response(503), UpstreamUnavailable),
        (httpx.Response(422, json={"detail": []}), UpstreamBadResponse),
        (httpx.Response(200, text="<html>gateway page</html>"), UpstreamBadResponse),
        (httpx.Response(200, json={"chunks": 1}), UpstreamBadResponse),
        (httpx.Response(200, json=["not", "an", "object"]), UpstreamBadResponse),
    ],
)
def test_nfr410_a_bad_answer_is_an_upstream_error(
    response: httpx.Response, error: type[UpstreamError]
) -> None:
    gw = FakeGateway()
    gw.translate = lambda request: response
    with pytest.raises(error):
        run(gw.client().translate("Apply online", REF))


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (httpx.ReadTimeout("slow"), UpstreamTimeout),
        (httpx.ConnectError("refused"), UpstreamUnavailable),
    ],
)
def test_nfr410_a_network_failure_is_an_upstream_error(
    failure: Exception, error: type[UpstreamError]
) -> None:
    gw = FakeGateway()

    def fail(request: httpx.Request) -> httpx.Response:
        raise failure

    gw.translate = fail
    with pytest.raises(error):
        run(gw.client().translate("Apply online", REF))


def test_nfr410_a_refused_token_request_is_an_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    config = Wso2Config(URL, TOKEN_URL, CLIENT_ID, SECRET, "model-x")
    client = Wso2Translator(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(UpstreamAuthError):
        run(client.translate("Apply online", REF))


# --- nothing secret or citizen-shaped escapes ---


def test_nfr303_no_secret_token_or_text_in_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Person-shaped but invented: distinctive tokens to look for (docs/CLAUDE.md).
    text = "Testperson Examplename applied"
    seen: list[str] = []
    for status in (401, 429, 500, 422):
        gw = FakeGateway()
        gw.translate = lambda request, s=status: httpx.Response(s, headers=TRACED)
        with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamError) as raised:
            run(gw.client().translate(text, REF))
        seen.append(str(raised.value))
    gw = FakeGateway()
    with caplog.at_level(logging.DEBUG):
        run(gw.client().translate(text, REF))
    exposed = "\n".join(seen) + "\n".join(r.getMessage() for r in caplog.records)
    for leaked in (SECRET, CLIENT_ID, "token-1", "token-2", "Testperson", "Examplename"):
        assert leaked not in exposed
    assert "request_id=" in exposed  # but GovTech can be given something to trace


def test_nfr303_the_config_never_prints_its_secret() -> None:
    config = Wso2Config(URL, TOKEN_URL, CLIENT_ID, SECRET, "model-x")
    assert SECRET not in repr(config) and SECRET not in str(config)
    assert SECRET not in repr(Settings.from_env({**_env(), **_wso2_env()}))


# --- wiring ---


def _env() -> dict[str, str]:
    return {
        "DZWEB_PG_DSN": "postgresql://x@127.0.0.1:1/x",
        "DZWEB_REDIS_URL": "redis://127.0.0.1:1/0",
        "DZWEB_TERMBASE": "termbase.json",
        "DZWEB_SITES": "sites.json",
    }


def _wso2_env() -> dict[str, str]:
    return {
        "DZWEB_TRANSLATOR": "wso2",
        "DZWEB_WSO2_URL": URL,
        "DZWEB_WSO2_TOKEN_URL": TOKEN_URL,
        "DZWEB_WSO2_CLIENT_ID": CLIENT_ID,
        "DZWEB_WSO2_CLIENT_SECRET": SECRET,
    }


def test_fr150_the_model_version_comes_from_configuration() -> None:
    """The API reports no version, so the cache keys rely on this setting."""
    from orchestrator.pipeline.segment import MODEL_FORMATS

    default = build_translator(Settings.from_env({**_env(), **_wso2_env()}), MODEL_FORMATS["wire"])
    assert isinstance(default, Wso2Translator)
    assert default.model_version == DEFAULT_MODEL_VERSION
    pinned = Settings.from_env({**_env(), **_wso2_env(), "DZWEB_MODEL_VERSION": "nllb-2026-11"})
    assert build_translator(pinned, MODEL_FORMATS["wire"]).model_version == "nllb-2026-11"


def test_fr100_settings_fill_in_from_dotenv_but_the_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\nDZWEB_WSO2_URL=https://from-file\nDZWEB_MODEL_VERSION=from-file\n\nnoise\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DZWEB_MODEL_VERSION", "from-environment")
    monkeypatch.delenv("DZWEB_WSO2_URL", raising=False)
    env = environment(dotenv)
    assert env["DZWEB_WSO2_URL"] == "https://from-file"
    assert env["DZWEB_MODEL_VERSION"] == "from-environment"


# --- against staging, only when asked ---


@pytest.mark.wso2
@pytest.mark.skipif(
    os.environ.get("DZWEB_WSO2_LIVE") != "1",
    reason="live GovTech API: set DZWEB_WSO2_LIVE=1 with credentials in .env",
)
def test_fr100_staging_translates_one_sentence() -> None:
    from orchestrator.pipeline.segment import MODEL_FORMATS

    settings = Settings.from_env({**_env(), **environment(), "DZWEB_TRANSLATOR": "wso2"})
    client = build_translator(settings, MODEL_FORMATS["wire"])
    sentence = "Apply for a passport online."
    out = run(client.translate(sentence, parse(sentence)))
    assert out.strip() and out != sentence


# --- one token for every process, until it expires (asked by GovTech, 2026-10-05) ---


class MemoryTokenStore:
    """What Redis does for RedisTokenStore, without Redis."""

    def __init__(self) -> None:
        self.value: tuple[str, float] | None = None
        self.puts: list[float] = []

    def get(self) -> tuple[str, float] | None:
        return self.value

    def put(self, token: str, seconds: float) -> None:
        self.puts.append(seconds)
        self.value = (token, seconds)

    def discard(self, token: str) -> None:
        if self.value and self.value[0] == token:
            self.value = None


class BrokenTokenStore:
    def get(self) -> Any:
        raise ConnectionError("redis down")

    put = discard = get  # type: ignore[assignment]


def test_fr155_processes_and_restarts_share_one_token() -> None:
    gw, store = FakeGateway(), MemoryTokenStore()
    run(gw.client(store).translate("api process", REF))
    worker = gw.client(store)
    run(worker.translate("worker process", REF))
    run(gw.client(store).translate("after a restart", REF))
    assert gw.tokens_issued == 1
    assert worker.tokens_reused == 1
    assert {r.headers["authorization"] for r in gw.translate_requests} == {"Bearer token-1"}


def test_fr155_the_shared_token_expires_a_minute_before_the_gateway_says() -> None:
    gw, store = FakeGateway(), MemoryTokenStore()
    run(gw.client(store).translate("text", REF))
    assert store.puts == [3600 - 60]


def test_fr155_an_expired_shared_token_is_not_used() -> None:
    gw, store = FakeGateway(), MemoryTokenStore()
    store.value = ("stale-token", 0.0)
    run(gw.client(store).translate("text", REF))
    assert gw.tokens_issued == 1
    assert gw.translate_requests[0].headers["authorization"] == "Bearer token-1"


def test_nfr410_a_refused_shared_token_is_replaced_for_everyone() -> None:
    gw, store = FakeGateway(), MemoryTokenStore()
    store.value = ("revoked-token", 1000.0)

    def refuse_revoked(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer revoked-token":
            return httpx.Response(401, json={"code": "900901"})
        return gw.ok(request)

    gw.translate = refuse_revoked
    assert run(gw.client(store).translate("text", REF)) == "DZ[text]"
    assert gw.tokens_issued == 1
    assert store.value is not None and store.value[0] == "token-1"
    run(gw.client(store).translate("another process", REF))
    assert gw.tokens_issued == 1  # the next process takes the new one


def test_nfr410_a_failing_token_store_never_fails_a_translation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gw = FakeGateway()
    with caplog.at_level(logging.WARNING):
        assert run(gw.client(BrokenTokenStore()).translate("text", REF)) == "DZ[text]"
    assert gw.tokens_issued == 1
    assert "token-1" not in caplog.text and SECRET not in caplog.text


@pytest.mark.integration
def test_fr155_redis_keeps_the_token_until_it_expires(redis_client: Any) -> None:
    from orchestrator.upstream.token_store import RedisTokenStore

    store = RedisTokenStore(redis_client, TOKEN_URL, CLIENT_ID)
    assert store.get() is None
    store.put("token-1", 120)
    token, seconds = store.get() or ("", 0.0)
    assert token == "token-1" and 119 < seconds <= 120  # noqa: S105 - a fake token
    assert SECRET not in str(redis_client.keys("*")) and CLIENT_ID not in store.key
    store.discard("some-other-token")
    assert store.get() is not None
    store.discard("token-1")
    assert store.get() is None
    store.put("short", 0.05)
    time.sleep(0.2)
    assert store.get() is None
