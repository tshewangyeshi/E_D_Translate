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
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from orchestrator.api.ratelimit import RateLimiter, client_bucket
from orchestrator.governance.paths import redact_path
from orchestrator.governance.sites import SiteRegistry
from orchestrator.service.translate import SegmentIn, Status, TranslateService
from orchestrator.store.reports import (
    MAX_COMMENT_CHARS,
    REASONS,
    ErrorReport,
    ReportStore,
    segment_is_saturated,
)

log = logging.getLogger(__name__)

MAX_SEGMENTS = 64
MAX_TEXT = 5000
MAX_BODY_BYTES = 512 * 1024


class SegmentRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=MAX_TEXT)
    selector_tier: int | None = None


class FeedbackRequest(BaseModel):
    """A citizen saying a translation is wrong (FR-430).

    `website` is a honeypot: a real reader never sees it, so anything that
    fills it is automated. It is named plausibly on purpose, because a field
    called `honeypot` is one a bot skips.
    """

    site: str = Field(min_length=1, max_length=128)
    segment_key: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str = "other"
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    website: str | None = None  # honeypot


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
    reports: ReportStore | None = None,
    feedback_limiter: RateLimiter | None = None,
) -> FastAPI:
    app = FastAPI(title="dzweb orchestrator", version="0.1", docs_url=None, redoc_url=None)
    origin_limiter = origin_limiter or RateLimiter(per_minute=6000, burst=600)
    client_limiter = client_limiter or RateLimiter(per_minute=120, burst=30)
    hasher = hasher or ClientHasher()
    # FR-432: 10 reports an hour per client. A burst of 10 so a reader who
    # spots several bad segments on one page can report them all at once.
    feedback_limiter = feedback_limiter or RateLimiter(per_minute=10 / 60, burst=10)

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

    @app.options("/v1/config")
    async def config_preflight(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is None or not sites.origin_enrolled(origin):
            return _error(403, "origin not enrolled")
        return Response(
            status_code=204,
            headers={
                **_cors(origin),
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, If-None-Match",
                "Access-Control-Max-Age": "600",
            },
        )

    @app.get("/v1/config")
    async def config(request: Request, site: str = "") -> Response:
        """Site configuration the widget needs before offering a toggle (FR-216).

        Selectors only: the widget uses them to mark blocks stricter and to skip
        private content. Path rules stay on the server, which is the only place
        the effective tier is decided (FR-512).
        """
        origin = request.headers.get("origin")
        enrolled = sites.allows(site, origin)
        if enrolled is None or origin is None:
            return _error(403, "origin not enrolled for this site")
        if not origin_limiter.allow(origin):
            return _error(
                429,
                "rate limit exceeded",
                {**_cors(origin), "Retry-After": str(origin_limiter.retry_after_seconds())},
            )
        payload = {
            "site": enrolled.site_id,
            "default_tier": enrolled.default_tier,
            "tier1_selectors": list(enrolled.tier1_selectors),
            "private_selectors": list(enrolled.private_selectors),
            "glossary_version": termbase_version,
            "max_segments": MAX_SEGMENTS,
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(content).hexdigest()[:32] + '"'
        headers = {**_cors(origin), "ETag": etag, "Cache-Control": "private, max-age=300"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content, media_type="application/json", headers=headers)

    @app.options("/v1/feedback")
    async def feedback_preflight(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is None or not sites.origin_enrolled(origin):
            return _error(403, "origin not enrolled")
        return Response(
            status_code=204,
            headers={
                **_cors(origin),
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
            },
        )

    @app.post("/v1/feedback")
    async def feedback(request: Request) -> Response:
        """Accept a citizen's report that a translation is wrong (FR-430, FR-432).

        Every outcome that is not a configuration error answers 202 with the
        same body. A report that was stored, one dropped for rate limiting, one
        dropped because the segment has had its fill today, and one dropped as
        a honeypot hit are indistinguishable from outside.

        That is the point. Distinguishable outcomes turn this endpoint into an
        oracle: a probe could map which segments are saturated, or tune itself
        against the limiter until it finds the edge. It also spares an honest
        reader who happens to trip a limit from being told their report did not
        count, which would teach them not to bother again.
        """
        origin = request.headers.get("origin")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "request too large")
        try:
            body = FeedbackRequest.model_validate(json.loads(raw))
        except (ValueError, UnicodeDecodeError, ValidationError):
            return _error(400, "invalid request")

        site = sites.allows(body.site, origin)
        if site is None or origin is None:
            return _error(403, "origin not enrolled for this site")

        accepted = Response(status_code=202, headers=_cors(origin))
        if reports is None:
            return accepted  # no store configured: accept and discard

        client = client_bucket(request.client.host if request.client else "unknown")
        # The hash decides whether to accept; it is never written down (NFR-303).
        limited = not feedback_limiter.allow(f"{origin}|{hasher.hash(client)}")
        honeypot = bool(body.website)
        saturated = segment_is_saturated(reports, body.segment_key, datetime.now(UTC))

        if limited or honeypot or saturated:
            log.info(
                "feedback dropped site=%s reason=%s",
                site.site_id,
                "honeypot" if honeypot else ("saturated" if saturated else "rate_limited"),
            )
            return accepted

        reports.record_report(
            ErrorReport(
                segment_key=body.segment_key,
                site_id=site.site_id,
                reason=body.reason if body.reason in REASONS else "other",
                comment=body.comment or None,
            )
        )
        log.info("feedback stored site=%s reason=%s", site.site_id, body.reason)
        return accepted

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
        # One IPv6 allocation is one client, for both the rate limit and the
        # distinct-client gate below: otherwise a new address buys a fresh burst
        # and counts as another citizen who has seen the text (NFR-304).
        client = client_bucket(request.client.host if request.client else "unknown")
        if not origin_limiter.allow(origin) or not client_limiter.allow(f"{origin}|{client}"):
            return _error(
                429,
                "rate limit exceeded",
                {**cors, "Retry-After": str(client_limiter.retry_after_seconds())},
            )

        segments = [SegmentIn(s.id, s.text, body.tier, s.selector_tier) for s in body.segments]
        results = await service.translate(site, hasher.hash(client), segments, body.path)

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

        # One line per request, carrying what an operator needs and nothing a
        # citizen would mind: the page with identifying segments replaced by
        # :id (NFR-304), counts by status, and no segment text or client
        # address anywhere (NFR-303).
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        log.info(
            "translate site=%s path=%s segments=%d %s",
            site.site_id,
            redact_path(body.path),
            len(results),
            " ".join(f"{name}={n}" for name, n in sorted(counts.items())),
        )
        headers = {**cors, "ETag": etag}
        if pending:
            headers["Cache-Control"] = "no-store"
        else:
            headers["Cache-Control"] = "private, max-age=60, must-revalidate"
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
        return Response(content, media_type="application/json; charset=utf-8", headers=headers)

    return app
