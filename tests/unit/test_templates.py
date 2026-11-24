"""Fallback templates: one per cause, default action, fixed confidence, always gate-compliant."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage.gatekeeper import check
from ecommerce_ai.agents.triage.model import CaseForTriage, EvidenceItem
from ecommerce_ai.agents.triage.policy import default_action
from ecommerce_ai.agents.triage.templates import fallback_draft
from ecommerce_ai.agents.triage_intake.model import TriageCause

OCCURRED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)

_EVIDENCE = {
    TriageCause.PAYMENT_FAILED: ("payments.payment-failed", "card declined"),
    TriageCause.INSUFFICIENT_STOCK: ("inventory.stock-rejected", "insufficient stock"),
    TriageCause.UNKNOWN_PRODUCT: ("inventory.stock-rejected", "unknown product"),
    TriageCause.CANCELLATION_REQUESTED: ("orders.order-cancelled", None),
    TriageCause.UNKNOWN: ("orders.order-cancelled", "something odd"),
}


def _case(cause: TriageCause, *, with_evidence: bool = True) -> CaseForTriage:
    order_id = uuid4()
    topic, reason = _EVIDENCE[cause]
    evidence = (
        (
            EvidenceItem(
                topic=topic,
                reason=reason,
                occurred_at=OCCURRED_AT,
                payload={"orderId": str(order_id), "reason": reason},
            ),
        )
        if with_evidence
        else ()
    )
    return CaseForTriage(case_id=uuid4(), order_id=order_id, cause=cause, evidence=evidence)


@pytest.mark.parametrize("cause", list(TriageCause))
@pytest.mark.parametrize("with_evidence", [True, False])
def test_should_pass_the_gatekeeper_when_used_for_a_case_of_its_cause(
    cause: TriageCause, with_evidence: bool
) -> None:
    case = _case(cause, with_evidence=with_evidence)

    assert check(fallback_draft(cause), case) == []


@pytest.mark.parametrize(
    ("cause", "confidence"),
    [
        (TriageCause.CANCELLATION_REQUESTED, 1.0),
        (TriageCause.PAYMENT_FAILED, 0.9),
        (TriageCause.INSUFFICIENT_STOCK, 0.9),
        (TriageCause.UNKNOWN_PRODUCT, 0.9),
        (TriageCause.UNKNOWN, 0.3),
    ],
)
def test_should_use_the_default_action_and_fixed_confidence_when_given_a_cause(
    cause: TriageCause, confidence: float
) -> None:
    draft = fallback_draft(cause)

    assert draft.suggested_action is default_action(cause)
    assert draft.confidence == confidence


def test_should_use_a_distinct_text_for_every_cause() -> None:
    texts = {fallback_draft(cause).explanation for cause in TriageCause}

    assert len(texts) == len(TriageCause)
