"""Share the GovTech access token between processes and across restarts.

Asked by GovTech, 2026-10-05: cache the token until it expires instead of
asking the token server again. Each Wso2Translator already keeps its token in
memory; this puts it where the API, the worker and every operator tool find
it, so a restart or a second process reuses the token instead of fetching one.

Stored: the access token alone, under a key derived from the token URL and
client id, with Redis expiring it when the client would stop using it (a
minute before the gateway does). Never the client secret. Losing the key, to
eviction or a flush, costs one token request and nothing else.
"""

from __future__ import annotations

import hashlib
from typing import Any

KEY_PREFIX = "dzweb:wso2-token:"


class RedisTokenStore:
    def __init__(self, client: Any, token_url: str, client_id: str) -> None:
        self.client = client
        digest = hashlib.sha256(f"{token_url}\n{client_id}".encode()).hexdigest()[:16]
        self.key = KEY_PREFIX + digest

    def get(self) -> tuple[str, float] | None:
        """The shared token and the seconds it may still be used, or None."""
        pipe = self.client.pipeline()
        pipe.get(self.key)
        pipe.pttl(self.key)
        value, ttl_ms = pipe.execute()
        if value is None or ttl_ms is None or ttl_ms <= 0:
            return None
        token = value.decode() if isinstance(value, bytes) else str(value)
        return token, ttl_ms / 1000.0

    def put(self, token: str, seconds: float) -> None:
        if seconds > 0:
            self.client.set(self.key, token, px=int(seconds * 1000))

    def discard(self, token: str) -> None:
        """Forget a token the gateway refused, unless another process replaced it already."""
        current = self.client.get(self.key)
        if (
            current is not None
            and (current.decode() if isinstance(current, bytes) else current) == token
        ):
            self.client.delete(self.key)
