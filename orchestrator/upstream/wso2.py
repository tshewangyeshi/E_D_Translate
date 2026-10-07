"""GovTech translation API through the WSO2 gateway (Sprint 0). Requirements: FR-100,
FR-155, FR-156, NFR-410.

    token:      POST DZWEB_WSO2_TOKEN_URL  grant_type=client_credentials, Basic auth
                → {"access_token", "token_type": "Bearer", "expires_in": 3600}
    translate:  POST DZWEB_WSO2_URL  {"text": ...}  Authorization: Bearer <token>
                → {"translated_text": "...", "chunks": N, "cached": bool}

Measured against staging on 2026-10-05 (tools/wso2_probe.py): 0.26-0.84 s a
call, a request id in ``x-dsai-request-id``, no model version anywhere in
the response, 401 for a bad token, 422 for empty text.

What this client promises the rest of the service:

* **One text per call.** The API takes no batch. Calls run in parallel up to
  ``concurrency``; the quota manager (FR-156) decides how many are made at all.
* **A token is fetched once and reused** until a minute before it expires,
  and fetched again once if the gateway refuses it. Concurrent callers share
  one fetch. With a ``token_store`` (Redis, wired in ``build``), other
  processes and the next restart reuse it too; GovTech asked for exactly this
  on 2026-10-05. If the store fails, the client fetches as it would without one.
* **Every failure is an UpstreamError**, which the request path turns into
  English now and a background retry (NFR-410). A translation call is safe to
  repeat, so the one retry after a refused token cannot double anything.
* **Nothing secret or citizen-shaped is logged or put in an exception**: not
  the secret, not the token, not the text. The gateway's request id is logged,
  so a failure can be traced with GovTech.

The model version is not reported by the API, so it comes from configuration
(``DZWEB_MODEL_VERSION``). It is part of every cache key (FR-150): when
GovTech changes the model, that setting must change with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from orchestrator.pipeline.segment import Segment
from orchestrator.upstream.errors import (
    UpstreamAuthError,
    UpstreamBadResponse,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)

log = logging.getLogger(__name__)

#: Fetch a new token this long before the old one expires.
TOKEN_MARGIN_SECONDS = 60.0
#: Per call. The live path has its own, shorter budget (FR-155) and cancels.
CALL_TIMEOUT_SECONDS = 30.0
DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class Wso2Config:
    url: str
    token_url: str
    client_id: str
    client_secret: str
    model_version: str
    concurrency: int = DEFAULT_CONCURRENCY
    timeout_seconds: float = CALL_TIMEOUT_SECONDS

    def __repr__(self) -> str:  # never print the secret, even by accident
        return f"Wso2Config(url={self.url!r}, model_version={self.model_version!r})"


class TokenStore(Protocol):
    """Where processes share a token (orchestrator/upstream/token_store.py)."""

    def get(self) -> tuple[str, float] | None: ...
    def put(self, token: str, seconds: float) -> None: ...
    def discard(self, token: str) -> None: ...


class Wso2Translator:
    """The ``Translator`` the service uses in production (orchestrator/upstream/translator.py)."""

    def __init__(
        self,
        config: Wso2Config,
        http: httpx.AsyncClient | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self.config = config
        self.model_version = config.model_version
        self.token_store = token_store
        self._owns_http = http is None
        self._http = http or self._new_http()
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock: asyncio.Lock | None = None
        self._slots: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.calls = 0
        self.token_fetches = 0
        self.tokens_reused = 0  # taken from the store instead of the token server

    async def translate(self, model_text: str, reference: Segment) -> str:
        if not model_text.strip():
            return model_text  # the API refuses empty text; nothing to translate anyway
        async with self._concurrency():
            self.calls += 1
            response = await self._post(model_text, await self._bearer())
            if response.status_code == 401:
                # Revoked or expired early: one fresh token, one more try.
                response = await self._post(model_text, await self._bearer(refresh=True))
                if response.status_code == 401:
                    raise UpstreamAuthError("the gateway refused a fresh token")
            return self._translated(response)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- token ----------------------------------------------------------------

    async def _bearer(self, refresh: bool = False) -> str:
        lock, _ = self._loop_bound()
        async with lock:
            refused = None
            if refresh and self._token is not None:
                refused, self._token = self._token, None
                self._shared("discard", refused)
            if self._token is None or time.monotonic() >= self._expires_at:
                if not self._adopt_shared(refused):
                    await self._fetch_token()
            assert self._token is not None
            return self._token

    def _adopt_shared(self, refused: str | None) -> bool:
        """Use the token another process (or the last run) stored, if it is still good."""
        shared = self._shared("get")
        if not shared:
            return False
        token, seconds = shared
        if seconds <= 0 or token == refused:
            return False
        self._token = token
        self._expires_at = time.monotonic() + seconds
        self.tokens_reused += 1
        return True

    def _shared(self, action: str, *args: Any) -> Any:
        """A token-store call that can never fail a translation."""
        if self.token_store is None:
            return None
        try:
            return getattr(self.token_store, action)(*args)
        except Exception as err:  # noqa: BLE001 - any store failure means: fetch as usual
            log.warning("token store %s failed: %s", action, type(err).__name__)
            return None

    async def _fetch_token(self) -> None:
        try:
            response = await self._http.post(
                self.config.token_url,
                data={"grant_type": "client_credentials"},
                auth=(self.config.client_id, self.config.client_secret),
            )
        except httpx.TimeoutException as err:
            raise UpstreamTimeout("token request timed out") from err
        except httpx.HTTPError as err:
            raise UpstreamUnavailable(f"token request failed: {type(err).__name__}") from err
        self.token_fetches += 1
        if response.status_code in (400, 401, 403):
            raise UpstreamAuthError(f"token request refused: HTTP {response.status_code}")
        if response.status_code != 200:
            raise UpstreamUnavailable(f"token request failed: HTTP {response.status_code}")
        grant = _json(response)
        token = grant.get("access_token")
        if not isinstance(token, str) or not token:
            raise UpstreamBadResponse("token response carried no access_token")
        lifetime = grant.get("expires_in")
        seconds = float(lifetime) if isinstance(lifetime, int | float) else 300.0
        usable = max(0.0, seconds - TOKEN_MARGIN_SECONDS)
        self._token = token
        self._expires_at = time.monotonic() + usable
        self._shared("put", token, usable)

    # -- translate ------------------------------------------------------------

    async def _post(self, text: str, token: str) -> httpx.Response:
        try:
            return await self._http.post(
                self.config.url,
                json={"text": text},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as err:
            raise UpstreamTimeout("translation request timed out") from err
        except httpx.HTTPError as err:
            raise UpstreamUnavailable(f"translation request failed: {type(err).__name__}") from err

    def _translated(self, response: httpx.Response) -> str:
        request_id = response.headers.get("x-dsai-request-id", "-")
        status = response.status_code
        if status == 429:
            log.warning("translation rate-limited request_id=%s", request_id)
            raise UpstreamRateLimited("gateway quota exhausted")
        if status >= 500:
            log.warning("translation failed status=%d request_id=%s", status, request_id)
            raise UpstreamUnavailable(f"HTTP {status}")
        if status != 200:
            # 4xx other than 401/429: what we sent was refused. Retrying will not help,
            # but the page still falls back to English like any other failure.
            log.warning("translation refused status=%d request_id=%s", status, request_id)
            raise UpstreamBadResponse(f"HTTP {status}")
        body = _json(response)
        translated = body.get("translated_text")
        if not isinstance(translated, str):
            raise UpstreamBadResponse("response carried no translated_text")
        log.debug(
            "translated request_id=%s chunks=%s cached=%s",
            request_id,
            body.get("chunks"),
            body.get("cached"),
        )
        return translated

    # -- event-loop-bound primitives -----------------------------------------

    def _new_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.config.timeout_seconds)

    def _loop_bound(self) -> tuple[asyncio.Lock, asyncio.Semaphore]:
        """asyncio primitives, and pooled connections, belong to one event loop.

        A server or the worker runs one loop for its whole life; tests and
        tools may run several in turn, so everything loop-bound is renewed
        when the loop changes. The token survives: it is just a string.
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._token_lock is None or self._slots is None:
            if self._loop is not None and self._owns_http:
                self._http = self._new_http()  # the old pool belonged to the old loop
            self._loop = loop
            self._token_lock = asyncio.Lock()
            self._slots = asyncio.Semaphore(max(1, self.config.concurrency))
        return self._token_lock, self._slots

    def _concurrency(self) -> asyncio.Semaphore:
        return self._loop_bound()[1]


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as err:
        raise UpstreamBadResponse("response was not JSON") from err
    if not isinstance(body, dict):
        raise UpstreamBadResponse("response was not a JSON object")
    return body


__all__ = ["UpstreamError", "Wso2Config", "Wso2Translator"]
