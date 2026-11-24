"""Suggested-action policy per cause (FR-006): pure functions, no I/O."""

from types import MappingProxyType
from typing import Final

from ecommerce_ai.agents.triage.model import SuggestedAction
from ecommerce_ai.agents.triage_intake.model import TriageCause

_DEFAULT_ACTIONS: Final = MappingProxyType(
    {
        TriageCause.PAYMENT_FAILED: SuggestedAction.CONTACT_CUSTOMER,
        TriageCause.INSUFFICIENT_STOCK: SuggestedAction.RESTOCK,
        TriageCause.UNKNOWN_PRODUCT: SuggestedAction.FIX_CATALOG,
        TriageCause.CANCELLATION_REQUESTED: SuggestedAction.NONE,
        TriageCause.UNKNOWN: SuggestedAction.REVIEW,
    }
)


def default_action(cause: TriageCause) -> SuggestedAction:
    """Return the action suggested for ``cause`` when nothing else is known."""
    return _DEFAULT_ACTIONS[cause]


def allowed_actions(cause: TriageCause) -> frozenset[SuggestedAction]:
    """Return the actions a result for ``cause`` may suggest: its default plus REVIEW."""
    return frozenset({default_action(cause), SuggestedAction.REVIEW})
