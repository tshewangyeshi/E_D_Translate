"""Run the orchestrator end to end against the mock translator, with no Docker.

    python tools/demo.py

Everything is in-memory (the same doubles the tests use), so this needs no
PostgreSQL, no Redis and no WSO2 access. The mock translator returns
``"DZ:" + English`` -- it is not Dzongkha. What this demonstrates is the
*pipeline* around the model: masking, glossary, tier gating and validation.

For the real thing (PostgreSQL, Redis, the worker), see CLAUDE.md.
"""

from __future__ import annotations

import io
import sys

from httpx import Response

from orchestrator.testing.rig import LEGAL_ORIGIN, ORIGIN, body, make_client, make_rig


def show(title: str, response: Response, *, note: str = "") -> None:
    print(f"\n--- {title} ---")
    if note:
        print(f"    {note}")
    data = response.json()
    if response.status_code != 200:
        print(f"    HTTP {response.status_code}: {data}")
        return
    for seg in data["segments"]:
        print(f"    status={seg['status']:<22} origin={seg.get('origin', '-')}")
        print(f"    text  = {seg['text']}")


def main() -> int:
    # Dzongkha and the placeholder brackets are not cp1252; without this the
    # Windows console raises UnicodeEncodeError on the first translated line.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    rig = make_rig(clients_to_persist=1)
    client = make_client(rig)
    headers = {"Origin": ORIGIN}

    print("Mock translator model_version:", rig.translator.model_version)
    print('Mock output is "DZ:" + the English it was given. Watch what survives it.')

    show(
        "1. Plain text",
        client.post("/v1/translate", json=body("Apply for a passport online."), headers=headers),
        note="Baseline: the mock prefixes DZ: and returns the rest untouched.",
    )

    show(
        "2. Entity masking (FR-140)",
        client.post(
            "/v1/translate",
            json=body("Pay Nu. 1,250 before 15 March 2026 at passport@example.gov.bt."),
            headers=headers,
        ),
        note="The amount, date and email are hidden from the model and restored byte-identical.",
    )

    show(
        "3. Inline markup (FR-121)",
        client.post(
            "/v1/translate",
            json=body("Read the ⟦1⟧renewal guide⟦/1⟧ first."),
            headers=headers,
        ),
        note="Placeholder pairs must come back in the same order or the block stays English.",
    )

    show(
        "4. Glossary term (FR-400)",
        client.post(
            "/v1/translate",
            json=body("Contact the Department of Immigration."),
            headers=headers,
        ),
        note="An approved term is substituted, not left to the model.",
    )

    show(
        "5. Tier 1 is never machine-translated (FR-510)",
        client.post(
            "/v1/translate",
            json=body("Fees are non-refundable.", site="legal"),
            headers={"Origin": LEGAL_ORIGIN},
        ),
        note="Legal text returns English until a human approves it. This is the invariant.",
    )

    show(
        "6. Unenrolled origin is refused (NFR-301)",
        client.post(
            "/v1/translate",
            json=body("Anything at all."),
            headers={"Origin": "https://not-enrolled.example"},
        ),
        note="Only origins listed in sites.json may call the service.",
    )

    print("\n--- 7. Caching (FR-150) ---")
    before = rig.translator.calls
    first = client.post("/v1/translate", json=body("Cached on the second ask."), headers=headers)
    after_first = rig.translator.calls
    second = client.post("/v1/translate", json=body("Cached on the second ask."), headers=headers)
    after_second = rig.translator.calls
    print(
        f"    first  request: status={first.json()['segments'][0]['status']}, "
        f"model called {after_first - before} time(s)"
    )
    print(
        f"    second request: status={second.json()['segments'][0]['status']}, "
        f"model called {after_second - after_first} time(s)  <- served from cache"
    )
    print(f"    same body, same ETag = {first.headers['ETag'] == second.headers['ETag']}")

    print("\nQueue depth (work deferred to the worker):", rig.queue.depth())
    return 0


if __name__ == "__main__":
    sys.exit(main())
