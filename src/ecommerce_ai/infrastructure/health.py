"""Dependency probes for readiness. They never block or fail startup (constitution IV)."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from ecommerce_ai.infrastructure.settings import Settings

PROBE_TIMEOUT_SECONDS = 1.0
"""Timeout of one connection attempt made by a readiness probe."""

POSTGRES_DEFAULT_PORT = 5432
"""Port used when ``DATABASE_URL`` does not name one."""


class DependencyStatus(StrEnum):
    """State of a dependency, or of the whole service."""

    UP = "UP"
    DOWN = "DOWN"


class DependencyProbe(Protocol):
    """Checks one dependency. A critical dependency that is down makes the service unready."""

    @property
    def name(self) -> str:
        """Name of the dependency in the readiness report."""
        ...

    @property
    def critical(self) -> bool:
        """Whether this dependency being down makes the service unready."""
        ...

    async def check(self) -> DependencyStatus:
        """Return the current state of the dependency; never raise."""
        ...


@dataclass(frozen=True, slots=True)
class TcpProbe:
    """Reports ``UP`` when a TCP connection to any of the endpoints opens within the timeout."""

    name: str
    endpoints: tuple[tuple[str, int], ...]
    critical: bool
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS

    async def check(self) -> DependencyStatus:
        """Try each endpoint in turn and close the connection at once."""
        for host, port in self.endpoints:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    _, writer = await asyncio.open_connection(host, port)
                    writer.close()
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                continue
            return DependencyStatus.UP
        return DependencyStatus.DOWN


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Overall readiness and the state of each dependency."""

    status: DependencyStatus
    dependencies: dict[str, DependencyStatus]


class ReadinessChecker:
    """Runs every probe concurrently and combines the results."""

    def __init__(self, probes: Sequence[DependencyProbe]) -> None:
        """Keep the probes to run on every check."""
        self._probes = tuple(probes)

    async def check(self) -> ReadinessReport:
        """``DOWN`` when any critical dependency is down; non-critical ones are only reported."""
        results = await asyncio.gather(*(probe.check() for probe in self._probes))
        ready = all(
            result is DependencyStatus.UP
            for probe, result in zip(self._probes, results, strict=True)
            if probe.critical
        )
        return ReadinessReport(
            status=DependencyStatus.UP if ready else DependencyStatus.DOWN,
            dependencies={
                probe.name: result for probe, result in zip(self._probes, results, strict=True)
            },
        )


def build_probes(settings: Settings) -> list[DependencyProbe]:
    """Probes for PostgreSQL (critical) and Kafka (reported only, never makes the service unready).

    The LLM provider probe arrives with the LLM client; it will be reported only, like Kafka.
    """
    database = urlsplit(settings.database_url.get_secret_value())
    brokers = tuple(
        (host, int(port)) for host, _, port in (b.rpartition(":") for b in settings.kafka_brokers)
    )
    return [
        TcpProbe(
            name="database",
            endpoints=((database.hostname or "localhost", database.port or POSTGRES_DEFAULT_PORT),),
            critical=True,
        ),
        TcpProbe(name="kafka", endpoints=brokers, critical=False),
    ]
