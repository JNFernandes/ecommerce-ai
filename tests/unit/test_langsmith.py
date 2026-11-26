"""LangSmith stays off by default and hides run inputs and outputs when turned on (US-08, US5)."""

import os
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langsmith import Client
from langsmith import utils as langsmith_utils

from ecommerce_ai.infrastructure.langsmith import (
    HIDE_INPUTS_VARIABLES,
    HIDE_OUTPUTS_VARIABLES,
    TRACING_VARIABLES,
    configure_langsmith,
)
from ecommerce_ai.infrastructure.settings import Settings

_STRAY_ON = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


class _State(TypedDict):
    count: int


def _increment(state: _State) -> _State:
    return {"count": state["count"] + 1}


def _run_tiny_graph() -> int:
    builder = StateGraph(_State)
    builder.add_node("increment", _increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    result = builder.compile().invoke({"count": 0})
    count: int = result["count"]
    return count


def test_should_disable_tracing_and_create_no_client_when_default_despite_stray_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()  # built before the stray values: they are not `Settings` choices
    for name in (*TRACING_VARIABLES, *HIDE_INPUTS_VARIABLES, *HIDE_OUTPUTS_VARIABLES):
        monkeypatch.setenv(name, "false")
    for name in _STRAY_ON:
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("LANGCHAIN_HIDE_INPUTS", "false")
    created: list[object] = []
    original_init = Client.__init__

    def _record_init(self: Client, *args: object, **kwargs: object) -> None:
        created.append(self)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]  # forwarded unchanged

    monkeypatch.setattr(Client, "__init__", _record_init)

    configure_langsmith(settings, os.environ)

    assert langsmith_utils.tracing_is_enabled() is False
    assert langsmith_utils.get_env_var("HIDE_INPUTS") == "true"
    assert _run_tiny_graph() == 1
    assert created == []


def test_should_hide_inputs_and_outputs_when_tracing_is_enabled() -> None:
    environ: dict[str, str] = {}

    configure_langsmith(Settings(langsmith_tracing=True), environ)

    assert {environ[name] for name in _STRAY_ON} == {"true"}
    assert environ["LANGSMITH_TRACING"] == "true"
    assert environ["LANGSMITH_HIDE_INPUTS"] == environ["LANGCHAIN_HIDE_INPUTS"] == "true"
    assert environ["LANGSMITH_HIDE_OUTPUTS"] == environ["LANGCHAIN_HIDE_OUTPUTS"] == "true"


def test_should_turn_tracing_on_for_langsmith_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(langsmith_tracing=True)
    # Set first so monkeypatch records them and removes what configure_langsmith writes.
    for name in (*TRACING_VARIABLES, *HIDE_INPUTS_VARIABLES, *HIDE_OUTPUTS_VARIABLES):
        monkeypatch.setenv(name, "false")

    configure_langsmith(settings, os.environ)

    assert langsmith_utils.tracing_is_enabled() is True
    assert langsmith_utils.get_env_var("HIDE_OUTPUTS") == "true"
