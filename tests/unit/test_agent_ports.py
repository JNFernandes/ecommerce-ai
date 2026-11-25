"""The shared agent ports stay the very objects the triage ports re-export."""

from ecommerce_ai.agents import ports as shared
from ecommerce_ai.agents.triage import ports as triage


def test_should_re_export_the_shared_ports_when_imported_from_triage() -> None:
    for name in (
        "Clock",
        "ModelError",
        "ModelOutputInvalid",
        "ModelUnavailable",
        "SystemClock",
        "TokenBudget",
    ):
        assert getattr(triage, name) is getattr(shared, name)


def test_should_treat_an_unavailable_model_as_a_model_error() -> None:
    error = shared.ModelUnavailable("timeout", input_tokens=3)

    assert isinstance(error, shared.ModelError)
    assert (error.input_tokens, error.output_tokens) == (3, 0)
