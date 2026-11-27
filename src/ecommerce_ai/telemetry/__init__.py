"""Telemetry: OpenTelemetry span helpers shared by every layer (constitution VII, US-08).

Cross-cutting like structlog: this package uses only the OpenTelemetry API, does no I/O and makes
no decisions. Span attributes come from the allowlist in ``attributes``; anything else is dropped
by the scrubbing exporter set up in ``infrastructure.tracing``. Without a provider every span is a
no-op.
"""
