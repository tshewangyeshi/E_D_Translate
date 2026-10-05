"""Upstream translation failures (FR-155, NFR-410).

The request path and the worker treat every one of these the same way: the
segment falls back to English now and is retried in the background. The
subclasses exist so metrics and logs can say which kind of failure it was.

Messages never carry the text being translated, a token or a credential.
"""

from __future__ import annotations


class UpstreamError(Exception):
    """The translation model could not be reached or did not answer usefully."""


class UpstreamTimeout(UpstreamError):
    pass


class UpstreamUnavailable(UpstreamError):
    """5xx from the model or the gateway."""

    status = 503


class UpstreamRateLimited(UpstreamError):
    """429: the gateway's quota for this client is spent for now."""

    status = 429


class UpstreamAuthError(UpstreamError):
    """The gateway refused our credentials, even with a fresh token."""

    status = 401


class UpstreamBadResponse(UpstreamError):
    """An answer we cannot use: not JSON, or no translated text in it."""
