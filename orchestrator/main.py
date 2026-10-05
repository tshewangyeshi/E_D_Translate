"""ASGI entry point: ``uvicorn orchestrator.main:create --factory`` (see orchestrator/wiring.py)."""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.app import create_app
from orchestrator.wiring import Settings, build, record_configuration


def create() -> FastAPI:
    c = build(Settings.from_env())
    record_configuration(c)  # FR-620: the API is the one writer of configuration changes
    return create_app(
        service=c.service,
        sites=c.sites,
        termbase_version=c.termbase.version,
        reports=c.reports,
        health=c.health,
        gauges=c.gauges,
        ops_token=c.settings.ops_token or None,
    )
