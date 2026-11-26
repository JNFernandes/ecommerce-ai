"""Every guard earns its place: without any one of them, some red-team case fails (SC-002)."""

import dataclasses

import pytest

from ecommerce_ai.agents.guardrails import Guardrails
from tests.redteam.loader import load_cases
from tests.redteam.runner import broken_expectations, production_guardrails, run_case

GUARDS = tuple(field.name for field in dataclasses.fields(Guardrails))


@pytest.mark.parametrize("guard", GUARDS)
async def test_should_fail_some_case_when_one_guard_is_replaced_by_its_noop(guard: str) -> None:
    noop = getattr(Guardrails.noop(), guard)
    weakened = dataclasses.replace(production_guardrails(), **{guard: noop})
    failing = [
        case.id
        for case in load_cases()
        if broken_expectations(case, await run_case(case, weakened))
    ]

    assert failing, f"no red-team case needs the {guard} guard"
