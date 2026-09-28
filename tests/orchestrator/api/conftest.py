"""Fixtures for the S2.1 API tests. Wiring lives in orchestrator.testing.rig (shared)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.testing.rig import Rig, make_client, make_rig


@pytest.fixture
def rig() -> Rig:
    return make_rig()


@pytest.fixture
def client(rig: Rig) -> TestClient:
    return make_client(rig)
