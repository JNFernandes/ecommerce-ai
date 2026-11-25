"""The red-team corpus against the real model (US-07 FR-024); opt-in with ``-m live_llm``.

Prints the pass rate per category and overall. Only ``must_not_call_tools`` and
``must_not_contain`` fail the test: the guards enforce those whatever the model does, while
refusals and finish reasons depend on how the real model answers.
"""

import os
from collections import Counter

import pytest
from pydantic import SecretStr

from ecommerce_ai.infrastructure.llm.anthropic_chat_model import build_anthropic_chat_model
from ecommerce_ai.infrastructure.llm.anthropic_model import build_anthropic_model
from ecommerce_ai.infrastructure.settings import Settings
from tests.redteam.loader import load_cases
from tests.redteam.runner import broken_expectations, run_case

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL = os.environ.get("ANTHROPIC_MODEL", "")
ENFORCED = frozenset({"must_not_call_tools", "must_not_contain", "blocked_reached_model"})

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(not (_API_KEY and _MODEL), reason="no ANTHROPIC_API_KEY/ANTHROPIC_MODEL"),
]


async def test_should_never_run_a_forbidden_tool_or_leak_when_the_real_model_is_attacked() -> None:
    settings = Settings(anthropic_api_key=SecretStr(_API_KEY), anthropic_model=_MODEL)
    _, client = build_anthropic_model(settings)
    model = build_anthropic_chat_model(settings, client)
    total: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    enforced_failures: list[tuple[str, list[str]]] = []
    try:
        for case in load_cases():
            broken = broken_expectations(case, await run_case(case, model=model))
            total[case.category] += 1
            passed[case.category] += not broken
            if ENFORCED.intersection(broken):
                enforced_failures.append((case.id, broken))
    finally:
        await client.close()

    for category in sorted(total):
        print(f"{category}: {passed[category]}/{total[category]}")
    print(f"overall: {sum(passed.values())}/{sum(total.values())}")
    assert enforced_failures == []
