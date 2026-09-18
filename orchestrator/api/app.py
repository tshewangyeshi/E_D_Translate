"""FastAPI application: ``POST /v1/translate`` (backlog S2.1).

Requirements: FR-100, FR-102, FR-103, FR-104, NFR-100, NFR-301, NFR-410, NFR-412.

    request ─► size limits (413) ─► JSON (application/json OR text/plain: no CORS preflight)
            ─► enrolled site + Origin allowlist (403) ─► rate limits per origin, per client (429)
            ─► TranslateService ─► 200 always for translation outcomes (NFR-410)
            ─► ETag = hash(body); no-store if any segment is pending_mt (FR-102);
               304 only when every segment is final
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from orchestrator.api.ratelimit import RateLimiter
from orchestrator.governance.sites import SiteRegistry
from orchestrator.service.translate import SegmentIn, Status, TranslateService

MAX_SEGMENTS = 64
MAX_TEXT = 5000
MAX_BODY_BYTES = 512 * 1024


class SegmentRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=MAX_TEXT)
    selector_tier: int | None = None


class TranslateRequest(BaseModel):
    source: str = "en"
    target: str = "dz"
    site: str = Field(min_length=1, max_length=128)
    path: str = Field(default="/", max_length=512)
    tier: Any = None  # hint only; never trusted (FR-512)
    segments: list[SegmentRequest] = Field(max_length=MAX_SEGMENTS)


@dataclass
class ClientHasher:
    """Salted, daily-rotated client hash (NFR-304). The salt never leaves memory."""

    today: Callable[[], date] = lambda: datetime.now(UTC).date()
    _day: date | None = None
    _salt: bytes = field(default=b"", repr=False)

    def hash(self, client: str) -> str:
        day = self.today()
        if day != self._day:
            self._day, self._salt = day, secrets.token_bytes(32)
        return hmac.new(self._salt, client.encode("utf-8"), hashlib.sha256).hexdigest()


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _cors(origin: str) -> dict[str, str]:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}


def create_app(
    *,
    service: TranslateService,
    sites: SiteRegistry,
    termbase_version: str,
    origin_limiter: RateLimiter | None = None,
    client_limiter: RateLimiter | None = None,
    hasher: ClientHasher | None = None,
) -> FastAPI:
    app = FastAPI(title="dzweb orchestrator", version="0.1", docs_url=None, redoc_url=None)
    origin_limiter = origin_limiter or RateLimiter(per_minute=6000, burst=600)
    client_limiter = client_limiter or RateLimiter(per_minute=120, burst=30)
    hasher = hasher or ClientHasher()

    @app.options("/v1/translate")
    async def preflight(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is None or not sites.origin_enrolled(origin):
            return _error(403, "origin not enrolled")
        return Response(
            status_code=204,
            headers={
                **_cors(origin),
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, If-None-Match",
                "Access-Control-Max-Age": "600",
            },
        )

    @app.post("/v1/translate")
    async def translate(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "request too large")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return _error(400, "body must be JSON")
        if isinstance(data, dict) and isinstance(data.get("segments"), list):
            if len(data["segments"]) > MAX_SEGMENTS:
                return _error(413, f"at most {MAX_SEGMENTS} segments per request")
            if any(
                isinstance(s, dict) and isinstance(s.get("text"), str) and len(s["text"]) > MAX_TEXT
                for s in data["segments"]
            ):
                return _error(413, f"segment text longer than {MAX_TEXT} characters")
        try:
            body = TranslateRequest.model_validate(data)
        except ValidationError:
            return _error(400, "invalid request")

        origin = request.headers.get("origin")
        site = sites.allows(body.site, origin)
        if site is None or origin is None:
            return _error(403, "origin not enrolled for this site")
        cors = _cors(origin)
        client = request.client.host if request.client else "unknown"
        if not origin_limiter.allow(origin) or not client_limiter.allow(f"{origin}|{client}"):
            return _error(
                429,
                "rate limit exceeded",
                {**cors, "Retry-After": str(client_limiter.retry_after_seconds())},
            )

        segments = [SegmentIn(s.id, s.text, body.tier, s.selector_tier) for s in body.segments]
        results = await service.translate(site, hasher.hash(client), segments)

        payload = {
            "model_version": service.translator.model_version,
            "glossary_version": termbase_version,
            "segments": [
                {
                    "id": r.id,
                    "segment_key": r.segment_key,
                    "text": r.text,
                    "status": r.status.value,
                    **({"origin": r.origin.value} if r.origin else {}),
                }
                for r in results
            ],
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(content).hexdigest()[:32] + '"'
        pending = any(r.status is Status.PENDING_MT for r in results)
        headers = {**cors, "ETag": etag}
        if pending:
            headers["Cache-Control"] = "no-store"
        else:
            headers["Cache-Control"] = "private, max-age=60, must-revalidate"
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
        return Response(content, media_type="application/json; charset=utf-8", headers=headers)

    return app
