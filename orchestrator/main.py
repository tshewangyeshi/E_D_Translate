"""ASGI entry point: ``uvicorn orchestrator.main:create --factory`` (see orchestrator/wiring.py)."""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.app import ClientHasher, create_app, shared_limiters
from orchestrator.wiring import Settings, build, record_configuration


def create() -> FastAPI:
    c = build(Settings.from_env())
    record_configuration(c)  # FR-620: the API is the one writer of configuration changes
    # One client hash and one set of rate limits for every replica (S2.1, S3.4).
    hasher = ClientHasher(key=c.settings.client_hash_key.encode())
    return create_app(
        hasher=hasher,
        **shared_limiters(c.redis, hasher),
        service=c.service,
        sites=c.sites,
        termbase_version=c.termbase.version,
        reports=c.reports,
        health=c.health,
        gauges=c.gauges,
        ops_token=c.settings.ops_token or None,
    )
