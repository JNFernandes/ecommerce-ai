"""Fake dependency probes: readiness tests never touch the network."""

from dataclasses import dataclass

from ecommerce_ai.infrastructure.health import DependencyStatus


@dataclass(frozen=True, slots=True)
class FakeProbe:
    """Reports a fixed status."""

    name: str
    critical: bool
    status: DependencyStatus

    async def check(self) -> DependencyStatus:
        """Return the configured status."""
        return self.status
