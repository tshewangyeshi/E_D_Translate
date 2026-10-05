"""FastAPI application: ``POST /v1/translate`` (backlog S2.1), feedback (S3.5), health and
metrics (S2.3).

Requirements: FR-100, FR-102, FR-103, FR-104, FR-430, FR-432, FR-610, FR-611, NFR-100,
NFR-301, NFR-303, NFR-410, NFR-412.

    request ─► size limits (413) ─► JSON (application/json OR text/plain: no CORS preflight)
            ─► enrolled site + Origin allowlist (403) ─► rate limits per origin, per client (429)
            ─► TranslateService ─► 200 always for translation outcomes (NFR-410)
            ─► ETag = hash(body); no-store if any segment is pending_mt (FR-102);
               304 only when every segment is final

``GET /v1/health`` and ``GET /v1/metrics`` are for operators, not for pages.
They answer 404 unless the request carries the operator token
(``DZWEB_OPS_TOKEN``), and 404 for everyone when no token is configured: a
deployment that forgets the gateway rule exposes nothing. They carry no CORS
headers, so a browser on another origin cannot read them either way.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from orchestrator.api.ratelimit import RateLimiter, client_bucket
from orchestrator.governance.paths import redact_path
from orchestrator.governance.sites import SiteRegistry
from orchestrator.ops import health as health_checks
from orchestrator.ops import metrics as ops_metrics
from orchestrator.service.translate import SegmentIn, Status, TranslateService
from orchestrator.store.reports import (
    MAX_COMMENT_CHARS,
    REASONS,
    ErrorReport,
    ReportStore,
    is_saturated,
    mask_comment,
)

log = logging.getLogger(__name__)

MAX_SEGMENTS = 64
MAX_TEXT = 5000
MAX_BODY_BYTES = 512 * 1024

#: Characters a comment may not carry: control characters other than the
#: line breaks and tabs a reader might type. PostgreSQL refuses NUL outright.
_CONTROL = {chr(c) for c in range(32)} - {"\n", "\r", "\t"} | {chr(127)}


class SegmentRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=MAX_TEXT)
    selector_tier: int | None = None


class FeedbackRequest(BaseModel):
    """A citizen saying a translation is wrong (FR-430).

    `website` is a honeypot: a real reader never sees it, so anything that
    fills it is automated. It is named plausibly on purpose, because a field
    called `honeypot` is one a bot skips.

    Everything that can be refused is refused here, from the request alone,
    before any outcome is decided. A 400 then says only "this request is
    malformed", whoever sends it and however full the limits are.
    """

    site: str = Field(min_length=1, max_length=128)
    segment_key: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="other", max_length=32)
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    website: str | None = Field(default=None, max_length=256)  # honeypot

    @field_validator("comment")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is not None and any(ch in _CONTROL for ch in value):
            raise ValueError("comment contains control characters")
        return value


class TranslateRequest(BaseModel):
    source: str = "en"
    target: str = "dz"
    site: str = Field(min_length=1, max_length=128)
    path: str = Field(default="/", max_length=512)
    tier: Any = None  # hint only; never trusted (FR-512)
    segments: list[SegmentRequest] = Field(max_length=MAX_SEGMENTS)


@dataclass
class ClientHasher:
    """Salted, daily-rotated client hash (NFR-304). The salt never leaves memory.

    The hash names its day (``YYYY-MM-DD:<hex>``) so the distinct-client
    counter files it under the day whose salt made it, even if the request
    finishes after midnight.
    """

    today: Callable[[], date] = lambda: datetime.now(UTC).date()
    _day: date | None = None
    _salt: bytes = field(default=b"", repr=False)

    def hash(self, client: str) -> str:
        day = self.today()
        if day != self._day:
            self._day, self._salt = day, secrets.token_bytes(32)
        digest = hmac.new(self._salt, client.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{day.isoformat()}:{digest}"


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _cors(origin: str) -> dict[str, str]:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}


def _parse_json(raw: bytes) -> Any:
    """JSON, or ValueError. A deeply nested body is malformed input, not a 500."""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as err:
        raise ValueError("body must be JSON") from err


#: What FastAPI answers for a route that does not exist. Operator routes answer
#: exactly this without the token, so probing learns nothing.
_NOT_FOUND = {"detail": "Not Found"}


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
    health: Mapping[str, health_checks.Probe] | None = None,
    gauges: ops_metrics.Gauges | None = None,
    ops_token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="dzweb orchestrator", version="0.1", docs_url=None, redoc_url=None)
    origin_limiter = origin_limiter or RateLimiter(per_minute=6000, burst=600)
    client_limiter = client_limiter or RateLimiter(per_minute=120, burst=30)
    hasher = hasher or ClientHasher()
    # FR-432: 10 reports an hour per client. A burst of 10 so a reader who
    # spots several bad segments on one page can report them all at once.
    feedback_limiter = feedback_limiter or RateLimiter(per_minute=10 / 60, burst=10)
    # In-memory checks answer inline; anything doing I/O -- the database and
    # cache from the wiring, and the queue -- runs bounded, off the loop (FR-610).
    checker = health_checks.HealthChecker(
        inline={
            "nmt": service.upstream.check,
            "quota": health_checks.quota_probe(service.quota),
        },
        blocking={"queue": health_checks.queue_probe(service.queue), **(health or {})},
    )
    gauge_reader = ops_metrics.GaugeReader(
        gauges if gauges is not None else ops_metrics.service_gauges(service)
    )

    def operator(request: Request) -> bool:
        if not ops_token:
            return False
        offered = request.headers.get("authorization", "")
        expected = f"Bearer {ops_token}"
        return hmac.compare_digest(offered.encode("utf-8"), expected.encode("utf-8"))

    @app.get("/v1/health")
    async def health_report(request: Request) -> Response:
        """Each upstream's state (FR-610). 200 when answered: see orchestrator/ops/health.py."""
        if not operator(request):
            return JSONResponse(_NOT_FOUND, status_code=404)
        body = health_checks.report(await checker.run())
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/v1/metrics")
    async def metrics_report(request: Request) -> Response:
        """Counters and rates in the Prometheus text format (FR-611)."""
        if not operator(request):
            return JSONResponse(_NOT_FOUND, status_code=404)
        families = ops_metrics.collect(service) + await asyncio.to_thread(gauge_reader.read)
        return Response(
            ops_metrics.render(families).encode("utf-8"),
            media_type=ops_metrics.CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    def preflight(methods: str, allow_headers: str) -> Callable[[Request], Any]:
        async def handler(request: Request) -> Response:
            origin = request.headers.get("origin")
            if origin is None or not sites.origin_enrolled(origin):
                return _error(403, "origin not enrolled")
            return Response(
                status_code=204,
                headers={
                    **_cors(origin),
                    "Access-Control-Allow-Methods": methods,
                    "Access-Control-Allow-Headers": allow_headers,
                    "Access-Control-Max-Age": "600",
                },
            )

        return handler

    app.options("/v1/translate")(preflight("POST, OPTIONS", "Content-Type, If-None-Match"))
    app.options("/v1/config")(preflight("GET, OPTIONS", "Content-Type, If-None-Match"))
    app.options("/v1/feedback")(preflight("POST, OPTIONS", "Content-Type"))

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

    @app.post("/v1/feedback")
    async def feedback(request: Request) -> Response:
        """Accept a citizen's report that a translation is wrong (FR-430, FR-432).

        Every outcome that is not a malformed or unenrolled request answers 202
        with the same empty body: stored, dropped as a honeypot hit, dropped
        for rate limiting, dropped because the segment or site has had its fill
        today, and not stored because the store is down.

        That is the point. Distinguishable outcomes turn this endpoint into an
        oracle: a probe could map which segments are saturated, or tune itself
        against the limiter until it finds the edge. It also spares an honest
        reader who happens to trip a limit from being told their report did not
        count, which would teach them not to bother again.

        The cheap refusals come first, so a caller over its limit costs no
        database work at all.
        """
        origin = request.headers.get("origin")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "request too large")
        try:
            body = FeedbackRequest.model_validate(_parse_json(raw))
        except (ValueError, ValidationError):
            return _error(400, "invalid request")

        site = sites.allows(body.site, origin)
        if site is None or origin is None:
            return _error(403, "origin not enrolled for this site")

        accepted = Response(status_code=202, headers=_cors(origin))
        if reports is None:
            return accepted  # no store configured: accept and discard
        # Normalised once, and only the normalised value goes anywhere: the
        # raw field is whatever the caller typed (NFR-303).
        reason = body.reason if body.reason in REASONS else "other"

        def dropped(why: str) -> Response:
            log.info("feedback dropped site=%s reason=%s why=%s", site.site_id, reason, why)
            return accepted

        if body.website:
            return dropped("honeypot")
        client = client_bucket(request.client.host if request.client else "unknown")
        # Keyed on the address bucket, like the translate limiter, and held in
        # memory only: never written down (NFR-303).
        if not origin_limiter.allow(origin) or not feedback_limiter.allow(f"{origin}|{client}"):
            return dropped("rate_limited")

        report = ErrorReport(
            segment_key=body.segment_key,
            site_id=site.site_id,
            reason=reason,
            comment=mask_comment(body.comment),
        )
        try:
            if is_saturated(reports, report, datetime.now(UTC)):
                return dropped("saturated")
            reports.record_report(report)
        except Exception as err:  # noqa: BLE001 - a store fault must look like any other outcome
            return dropped(f"store_error:{type(err).__name__}")
        log.info("feedback stored site=%s reason=%s", site.site_id, reason)
        return accepted

    @app.post("/v1/translate")
    async def translate(request: Request) -> Response:
        started = time.perf_counter()
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "request too large")
        try:
            data = _parse_json(raw)
        except ValueError:
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
        # Requests that reached the service, whatever it answered: refusals
        # (403, 413, 429) cost nothing and would flatter the figure (FR-611).
        service.metrics.latency.observe(time.perf_counter() - started)
        headers = {**cors, "ETag": etag}
        if pending:
            headers["Cache-Control"] = "no-store"
        else:
            headers["Cache-Control"] = "private, max-age=60, must-revalidate"
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
        return Response(content, media_type="application/json; charset=utf-8", headers=headers)

    return app
