"""ASGI entry point: ``uvicorn orchestrator.main:create --factory`` (see orchestrator/wiring.py)."""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.app import create_app
from orchestrator.wiring import Settings, build


def create() -> FastAPI:
    c = build(Settings.from_env())
    return create_app(service=c.service, sites=c.sites, termbase_version=c.termbase.version)
