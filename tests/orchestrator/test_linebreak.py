"""Line-break assistance (S6.2). Requirements: FR-160, FR-330, NFR-500 · [ER-8]

Zero-width spaces let a browser wrap Dzongkha, which has no spaces between
words. They are a rendering aid only: nothing stored -- cache, translation
memory, anything speech would read -- may carry one, whoever put it there,
the model included. And the server and the widget insert them at the same
places, proved by one fixture both run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.locale.dz import RENDER_ARTEFACTS, insert_breaks, strip_render_artefacts
from orchestrator.pipeline.segment import Segment
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "linebreak" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fr160_breaks_go_where_the_shared_fixture_says(case: dict[str, str]) -> None:
    assert insert_breaks(case["input"]) == case["rendered"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fr160_stripping_recovers_the_stored_text(case: dict[str, str]) -> None:
    assert strip_render_artefacts(case["rendered"]) == strip_render_artefacts(case["input"])
    assert not set(strip_render_artefacts(case["rendered"])) & RENDER_ARTEFACTS


class Answers:
    """A model that writes zero-width spaces of its own, as some Dzongkha text carries."""

    model_version = "test"

    async def translate(self, model_text: str, reference: Segment) -> str:
        return "\u0f40\u0f0b\u200b\u0f41\u200c\u0f42\ufeff"


def test_fr330_nothing_stored_carries_a_rendering_artefact() -> None:
    rig = make_rig()
    rig.service.translator = Answers()  # type: ignore[assignment]
    response = make_client(rig).post(
        "/v1/translate", json=body("Apply online now."), headers={"Origin": ORIGIN}
    )
    (segment,) = response.json()["segments"]
    assert segment["status"] == "translated"

    stored: list[Any] = [v.masked_target for v in rig.tm._versions.values()]
    stored += list(rig.cache.data.values())
    assert stored, "nothing was stored"
    for value in stored:
        assert not set(str(value)) & RENDER_ARTEFACTS, (
            f"stored with a rendering artefact: {value!r}"
        )
    # What is served is the stored text: the widget adds its own breaks on the page.
    assert not set(segment["text"]) & RENDER_ARTEFACTS
