"""The deterministic guards are cheap: input + output checks of 2000-char worst cases (SC-008)."""

import statistics
import time

from ecommerce_ai.agents.assistant.gatekeeper import ChatEvidence, check_answer
from ecommerce_ai.agents.guardrails import OutputContext
from ecommerce_ai.agents.guardrails.input_rules import RuleInputGuard, load_input_rules
from ecommerce_ai.agents.guardrails.output_checks import LeakChecks

RUNS = 100
P95_LIMIT_MS = 50.0
MAX_CHARS = 2000
WORST_INPUTS = (
    # Near-misses keep every pattern scanning to the end; the encoded runs force the decodes.
    ("ignore the previous " * 100)[:MAX_CHARS],
    ("aGVsbG8gd29ybGQgaGVsbG8gd29ybGQg" * 70)[:MAX_CHARS],
    ("i.g.n.o.r.e " * 200)[:MAX_CHARS],
    (("\u200b" + "".join(chr(0xFF21 + ord(c) - ord("A")) for c in "IGNORE") + " ") * 300)[
        :MAX_CHARS
    ],
)
WORST_ANSWER = (
    "Your order 3f2a4c1e-9b8d-4e7f-a6b5-1c2d3e4f5a6b ships soon. sk-not-a-key eyJhbGc host:5436 "
    * 30
)[:MAX_CHARS]


def _p95_ms(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1] * 1000


def test_should_check_input_and_output_within_fifty_ms_at_p95_when_texts_are_worst_case() -> None:
    guard = RuleInputGuard(load_input_rules())
    checks = LeakChecks()
    context = OutputContext(canary="cnry-" + "0" * 24, system_prompt="You are a helpful assistant.")
    evidence = ChatEvidence(
        data=["3f2a4c1e-9b8d-4e7f-a6b5-1c2d3e4f5a6b"],
        customer_texts=["Where is my order?"],
        output_checks=checks,
        output_context=context,
    )
    samples: list[float] = []
    for run in range(RUNS):
        text = WORST_INPUTS[run % len(WORST_INPUTS)]
        started = time.perf_counter()
        guard.check(text)
        check_answer(WORST_ANSWER, evidence)
        samples.append(time.perf_counter() - started)

    assert _p95_ms(samples) < P95_LIMIT_MS
