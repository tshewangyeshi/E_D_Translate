"""Translator interface (FR-100). The WSO2 client implements this once access exists (S0.1).

``translate`` receives text already encoded in the model token format and
returns the model's raw output in the same format. It raises
:class:`orchestrator.testing.mock_nmt.UpstreamError` (or a subclass) on
upstream failure.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from orchestrator.pipeline.segment import ModelFormat, Segment
from orchestrator.testing.mock_nmt import MockNMT, Mode


class Translator(Protocol):
    model_version: str

    async def translate(self, model_text: str, reference: Segment) -> str: ...


class MockTranslator:
    """Adversarial mock behind the Translator interface, with optional latency."""

    def __init__(
        self,
        fmt: ModelFormat,
        *,
        model_version: str = "mock-nllb-1",
        seed: int = 0,
        modes: dict[Mode, float] | None = None,
        delay_seconds: float = 0.0,
        prefix: str = "DZ:",
    ) -> None:
        self.model_version = model_version
        self.delay_seconds = delay_seconds
        self.calls = 0
        self._mock = MockNMT(
            fmt,
            seed=seed,
            modes=modes,
            translate_text=lambda t: f"{prefix}{t}" if t.strip() else t,
        )

    async def translate(self, model_text: str, reference: Segment) -> str:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self._mock.translate(reference)
