"""TCP readiness probes, against loopback sockets only."""

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest

from ecommerce_ai.infrastructure.health import (
    DependencyStatus,
    ReadinessChecker,
    TcpProbe,
    build_probes,
)
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.probes import FakeProbe


async def _close_at_once(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


@pytest.fixture
async def open_port() -> AsyncIterator[int]:
    server = await asyncio.start_server(_close_at_once, "127.0.0.1", 0)
    async with server:
        port: int = server.sockets[0].getsockname()[1]
        yield port


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


async def test_should_report_up_when_the_port_accepts_connections(open_port: int) -> None:
    probe = TcpProbe("database", (("127.0.0.1", open_port),), critical=True)

    assert await probe.check() is DependencyStatus.UP


async def test_should_report_down_when_no_endpoint_accepts_connections() -> None:
    probe = TcpProbe("kafka", (("127.0.0.1", _closed_port()),), critical=False)

    assert await probe.check() is DependencyStatus.DOWN


async def test_should_report_up_when_any_broker_accepts_connections(open_port: int) -> None:
    endpoints = (("127.0.0.1", _closed_port()), ("127.0.0.1", open_port))
    probe = TcpProbe("kafka", endpoints, critical=False)

    assert await probe.check() is DependencyStatus.UP


async def test_should_be_unready_only_for_a_critical_dependency() -> None:
    checker = ReadinessChecker(
        [
            FakeProbe("database", True, DependencyStatus.UP),
            FakeProbe("kafka", False, DependencyStatus.DOWN),
        ]
    )

    report = await checker.check()

    assert report.status is DependencyStatus.UP
    assert report.dependencies == {"database": DependencyStatus.UP, "kafka": DependencyStatus.DOWN}


def test_should_probe_the_configured_database_and_brokers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.local/ai")
    monkeypatch.setenv("KAFKA_BROKERS", "k1:9092,k2:9093")

    database, kafka = build_probes(Settings())

    assert isinstance(database, TcpProbe)
    assert isinstance(kafka, TcpProbe)
    assert (database.critical, database.endpoints) == (True, (("db.local", 5432),))
    assert (kafka.critical, kafka.endpoints) == (False, (("k1", 9092), ("k2", 9093)))
