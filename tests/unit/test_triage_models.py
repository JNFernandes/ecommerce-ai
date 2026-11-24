"""Validation of the triage domain models (data-model.md)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ecommerce_ai.agents.triage.model import (
    EXPLANATION_MAX_CHARS,
    CaseForTriage,
    DraftOutput,
    EvidenceItem,
    GateRule,
    ResultSource,
    SuggestedAction,
    TriageResult,
    Violation,
)
from ecommerce_ai.agents.triage_intake.model import TriageCause


def _result(**overrides: object) -> TriageResult:
    values: dict[str, object] = {
        "cause": TriageCause.PAYMENT_FAILED,
        "explanation": "The payment was declined, so the order was cancelled.",
        "suggested_action": SuggestedAction.CONTACT_CUSTOMER,
        "confidence": 0.8,
        "source": ResultSource.LLM,
        "prompt_version": "triage.v1",
        "model_id": "claude-opus-5-5",
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 900,
        "revisions": 0,
        "violations": (),
    }
    values.update(overrides)
    return TriageResult.model_validate(values)


def test_should_accept_a_draft_when_every_field_is_within_bounds() -> None:
    draft = DraftOutput(
        explanation="x" * EXPLANATION_MAX_CHARS,
        suggested_action=SuggestedAction.REVIEW,
        confidence=1.0,
    )

    assert draft.suggested_action is SuggestedAction.REVIEW


@pytest.mark.parametrize(
    "overrides",
    [
        {"explanation": ""},
        {"explanation": "x" * (EXPLANATION_MAX_CHARS + 1)},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"suggested_action": "REFUND"},
        {"extra": "field"},
    ],
)
def test_should_reject_a_draft_when_a_field_is_out_of_bounds_or_unknown(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "explanation": "Fine.",
        "suggested_action": "REVIEW",
        "confidence": 0.5,
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        DraftOutput.model_validate(values)


def test_should_reject_a_draft_when_it_is_modified() -> None:
    draft = DraftOutput(explanation="Fine.", suggested_action=SuggestedAction.NONE, confidence=1)

    with pytest.raises(ValidationError):
        draft.confidence = 0.1  # type: ignore[misc]  # frozen on purpose: this is the test


def test_should_keep_the_evidence_of_a_case_when_built() -> None:
    item = EvidenceItem(
        topic="payments.payment-failed",
        reason="card declined",
        occurred_at=datetime(2026, 11, 24, tzinfo=UTC),
        payload={"amount": "10.00"},
    )
    case = CaseForTriage(
        case_id=uuid4(), order_id=uuid4(), cause=TriageCause.PAYMENT_FAILED, evidence=(item,)
    )

    assert case.evidence == (item,)


def test_should_reject_a_violation_when_its_attempt_is_not_positive() -> None:
    with pytest.raises(ValidationError):
        Violation(rule=GateRule.PROMISE, message="Promises a refund.", attempt=0)


def test_should_accept_a_result_when_an_llm_result_has_a_model_id() -> None:
    violation = Violation(rule=GateRule.LENGTH, message="Too long.", attempt=1)

    result = _result(revisions=1, violations=(violation,))

    assert result.violations == (violation,)


def test_should_accept_a_result_when_a_fallback_has_no_model_id() -> None:
    result = _result(source=ResultSource.FALLBACK_TEMPLATE, model_id=None)

    assert result.model_id is None


@pytest.mark.parametrize(
    ("source", "model_id"),
    [(ResultSource.LLM, None), (ResultSource.FALLBACK_TEMPLATE, "claude-opus-5-5")],
)
def test_should_reject_a_result_when_model_id_does_not_match_the_source(
    source: ResultSource, model_id: str | None
) -> None:
    with pytest.raises(ValidationError, match="model_id"):
        _result(source=source, model_id=model_id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"revisions": 3},
        {"revisions": -1},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
        {"confidence": 2},
        {"explanation": ""},
        {"prompt_version": ""},
    ],
)
def test_should_reject_a_result_when_a_field_is_out_of_bounds(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _result(**overrides)
