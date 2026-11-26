"""LangSmith configuration: off by default, and run inputs and outputs hidden when on (US-08, D12).

LangGraph sends runs to LangSmith when the environment says so. This module makes the service's
settings the only switch: at startup it writes the variables LangSmith reads, overriding any stray
value a shell or container left behind, so nothing leaves the process unless
``LANGSMITH_TRACING=true`` was chosen on purpose.
"""

from collections.abc import MutableMapping
from typing import Final

import structlog
from langsmith import utils as langsmith_utils

from ecommerce_ai.infrastructure.settings import Settings

# Every name LangSmith checks to turn tracing on, in both its namespaces (``*_TRACING_V2`` wins
# over ``*_TRACING``, ``LANGSMITH_*`` over ``LANGCHAIN_*``): all of them are written.
TRACING_VARIABLES: Final = (
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING",
)
HIDE_INPUTS_VARIABLES: Final = ("LANGSMITH_HIDE_INPUTS", "LANGCHAIN_HIDE_INPUTS")
HIDE_OUTPUTS_VARIABLES: Final = ("LANGSMITH_HIDE_OUTPUTS", "LANGCHAIN_HIDE_OUTPUTS")

_log = structlog.get_logger(__name__)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def clear_langsmith_env_cache() -> None:
    """Forget the environment values LangSmith cached, so the next read sees the current ones."""
    # LangSmith types ``get_env_var`` as an overload, hiding the ``lru_cache`` it is wrapped in.
    langsmith_utils.get_env_var.cache_clear()  # type: ignore[attr-defined]


def configure_langsmith(settings: Settings, environ: MutableMapping[str, str]) -> None:
    """Write LangSmith's tracing and hide variables into ``environ`` from ``settings``.

    Tracing follows ``langsmith_tracing`` (default off); the hide flags are written whether
    tracing is on or not, so turning it on never exposes message text by accident. LangSmith
    caches the variables it reads, so the cache is cleared afterwards.
    """
    for name in TRACING_VARIABLES:
        environ[name] = _flag(settings.langsmith_tracing)
    for name in HIDE_INPUTS_VARIABLES:
        environ[name] = _flag(settings.langsmith_hide_inputs)
    for name in HIDE_OUTPUTS_VARIABLES:
        environ[name] = _flag(settings.langsmith_hide_outputs)
    clear_langsmith_env_cache()
    if settings.langsmith_tracing:
        _log.info(
            "langsmith_tracing_enabled",
            hide_inputs=settings.langsmith_hide_inputs,
            hide_outputs=settings.langsmith_hide_outputs,
        )
