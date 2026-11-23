"""Composition root: wires settings, logging, probes and the API into one application.

Run with ``python -m ecommerce_ai`` (reads ``HOST`` and ``PORT``) or
``uvicorn ecommerce_ai.main:create_app --factory``. Nothing here connects to Kafka, PostgreSQL or
the LLM provider, so the service starts without any of them (constitution IV).
"""

from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from ecommerce_ai.api.app import build_api
from ecommerce_ai.infrastructure.health import DependencyProbe, ReadinessChecker, build_probes
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings


def create_app(
    settings: Settings | None = None, probes: Sequence[DependencyProbe] | None = None
) -> FastAPI:
    """Build the application; ``probes`` replaces the real dependency probes in tests."""
    settings = settings or Settings()
    configure_logging(settings.log_level.value)
    checker = ReadinessChecker(build_probes(settings) if probes is None else probes)
    return build_api(settings.cors_allowed_origins, checker)


def run() -> None:
    """Serve the application on ``HOST``:``PORT`` with uvicorn."""
    settings = Settings()
    uvicorn.run(
        "ecommerce_ai.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
