"""Fallback explanations, one per cause (FR-010, research 9): fixed text, no I/O.

Used whenever no gated LLM draft is available. Each template states no amount, id, date or
status the evidence must ground, so it passes the gatekeeper for any case of its cause.
"""

from types import MappingProxyType
from typing import Final

from ecommerce_ai.agents.triage.model import DraftOutput
from ecommerce_ai.agents.triage.policy import default_action
from ecommerce_ai.agents.triage_intake.model import TriageCause

_TEMPLATES: Final = MappingProxyType(
    {
        TriageCause.PAYMENT_FAILED: (
            "The payment for this order was not accepted, so the order could not go ahead. "
            "Contact the customer about another way to pay.",
            0.9,
        ),
        TriageCause.INSUFFICIENT_STOCK: (
            "There was not enough stock to reserve every item of this order, so the order could "
            "not go ahead. Restocking the items may help later orders.",
            0.9,
        ),
        TriageCause.UNKNOWN_PRODUCT: (
            "This order contained a product the catalogue does not know, so its stock could not "
            "be reserved. Check the catalogue entry of that product.",
            0.9,
        ),
        TriageCause.CANCELLATION_REQUESTED: (
            "The customer asked to stop this order before it went ahead. No action is needed.",
            1.0,
        ),
        TriageCause.UNKNOWN: (
            "The stored events do not show clearly why this order failed. "
            "Review the order by hand.",
            0.3,
        ),
    }
)


def fallback_draft(cause: TriageCause) -> DraftOutput:
    """The fixed explanation for ``cause``, with its default action and fixed confidence."""
    explanation, confidence = _TEMPLATES[cause]
    return DraftOutput(
        explanation=explanation, suggested_action=default_action(cause), confidence=confidence
    )
