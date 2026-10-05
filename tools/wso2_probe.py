"""One-off probe of the GovTech translation API (Sprint 0). Requirements: FR-100, FR-155.

    python tools/wso2_probe.py

Learns what the onboarding email did not say: the response format, how long
a token lasts, how long a call takes, whether a model version is reported,
and what an error looks like. About ten calls in all.

Credentials come from the environment or from ``.env`` (git-ignored). The
token and the secret are never printed: only their length and the names of
the fields around them.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]

#: Invented, public-page-style sentences. Never real citizen data (docs/CLAUDE.md).
SENTENCES = [
    "submission of application",
    "Apply for a passport online.",
    "Pay Nu. 500 by 30 June 2026.",
    "Visit the Department of Immigration for more information.",
]

#: Headers worth knowing about: versions, limits, tracing. Never auth headers.
INTERESTING = ("content-type", "x-", "ratelimit", "retry-after", "version", "model", "date")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip())
    return env


def shape(value: Any, depth: int = 0) -> Any:
    """The structure of a JSON value: keys and types, strings shortened."""
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v, depth + 1) for v in value[:3]] + (["..."] if len(value) > 3 else [])
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "..."
    return value


def headers_of(response: httpx.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in response.headers.items()
        if any(k.lower().startswith(p) or p in k.lower() for p in INTERESTING)
        and "authorization" not in k.lower()
        and "cookie" not in k.lower()
    }


def body_of(response: httpx.Response) -> Any:
    try:
        return shape(response.json())
    except ValueError:
        return response.text[:500]


def main() -> int:
    env = load_env()
    needed = [
        "DZWEB_WSO2_URL",
        "DZWEB_WSO2_TOKEN_URL",
        "DZWEB_WSO2_CLIENT_ID",
        "DZWEB_WSO2_CLIENT_SECRET",
    ]
    missing = [k for k in needed if not env.get(k)]
    if missing:
        print(f"missing in .env or the environment: {', '.join(missing)}", file=sys.stderr)
        return 2

    with httpx.Client(timeout=30.0) as http:
        print("== token ==")
        started = time.perf_counter()
        token_response = http.post(
            env["DZWEB_WSO2_TOKEN_URL"],
            data={"grant_type": "client_credentials"},
            auth=(env["DZWEB_WSO2_CLIENT_ID"], env["DZWEB_WSO2_CLIENT_SECRET"]),
        )
        print(f"status {token_response.status_code} in {time.perf_counter() - started:.2f}s")
        print("headers", headers_of(token_response))
        if token_response.status_code != 200:
            print("body", body_of(token_response))
            return 1
        grant = token_response.json()
        token = grant.get("access_token", "")
        print(
            "fields",
            {k: (f"<{len(v)} chars>" if k == "access_token" else v) for k, v in grant.items()},
        )

        print("\n== translate ==")
        for text in SENTENCES:
            started = time.perf_counter()
            response = http.post(
                env["DZWEB_WSO2_URL"],
                json={"text": text},
                headers={"Authorization": f"Bearer {token}"},
            )
            elapsed = time.perf_counter() - started
            print(f"\n{text!r}: status {response.status_code} in {elapsed:.2f}s")
            print("headers", headers_of(response))
            print("body", json.dumps(body_of(response), ensure_ascii=False))

        print("\n== errors ==")
        cases: list[tuple[str, dict[str, str], Any]] = [
            ("bad token", {"Authorization": "Bearer not-a-real-token"}, {"text": "test"}),
            ("empty text", {"Authorization": f"Bearer {token}"}, {"text": ""}),
            ("wrong field", {"Authorization": f"Bearer {token}"}, {"sentence": "test"}),
        ]
        for name, headers, payload in cases:
            response = http.post(env["DZWEB_WSO2_URL"], json=payload, headers=headers)
            print(f"\n{name}: status {response.status_code}")
            print("body", json.dumps(body_of(response), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
