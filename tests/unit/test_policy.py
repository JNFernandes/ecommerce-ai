"""Default and allowed suggested actions per cause (FR-006)."""

import pytest

from ecommerce_ai.agents.triage.model import SuggestedAction
from ecommerce_ai.agents.triage.policy import allowed_actions, default_action
from ecommerce_ai.agents.triage_intake.model import TriageCause


@pytest.mark.parametrize(
    ("cause", "action"),
    [
        (TriageCause.PAYMENT_FAILED, SuggestedAction.CONTACT_CUSTOMER),
        (TriageCause.INSUFFICIENT_STOCK, SuggestedAction.RESTOCK),
        (TriageCause.UNKNOWN_PRODUCT, SuggestedAction.FIX_CATALOG),
        (TriageCause.CANCELLATION_REQUESTED, SuggestedAction.NONE),
        (TriageCause.UNKNOWN, SuggestedAction.REVIEW),
    ],
)
def test_should_derive_the_default_action_when_given_a_cause(
    cause: TriageCause, action: SuggestedAction
) -> None:
    assert default_action(cause) is action


@pytest.mark.parametrize("cause", list(TriageCause))
def test_should_allow_the_default_action_and_review_when_given_a_cause(
    cause: TriageCause,
) -> None:
    assert allowed_actions(cause) == {default_action(cause), SuggestedAction.REVIEW}


def test_should_allow_only_review_when_the_cause_is_unknown() -> None:
    assert allowed_actions(TriageCause.UNKNOWN) == {SuggestedAction.REVIEW}
