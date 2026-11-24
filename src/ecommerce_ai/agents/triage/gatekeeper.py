"""Deterministic gatekeeper of every draft (FR-008, research 8): pure functions, no I/O.

``check`` returns one ``Violation`` per broken rule, each with a fixed message that never quotes
the draft or the evidence. Facts (amounts and numbers, UUIDs, ISO and written dates, order status
words) must be found in the case's evidence: payload values, reasons, times and topics. The shared
rules live in ``agents/safety/rules.py``; length, plain text and the allowed action are triage's.
"""

import re
from collections.abc import Iterable, Iterator
from types import MappingProxyType
from typing import Final

from ecommerce_ai.agents.safety.rules import (
    Evidence,
    facts_grounded,
    has_contact_or_url,
    has_internal_name,
    has_promise,
    json_leaves,
)
from ecommerce_ai.agents.triage.model import (
    EXPLANATION_MAX_CHARS,
    CaseForTriage,
    DraftOutput,
    GateRule,
    Violation,
)
from ecommerce_ai.agents.triage.policy import allowed_actions

MAX_SENTENCES: Final = 3
"""Most sentences an explanation may have."""

RULE_MESSAGES: Final = MappingProxyType(
    {
        GateRule.PROMISE: (
            "Do not promise or announce a refund, compensation, credit, voucher, discount, "
            "delivery or any other future action."
        ),
        GateRule.UNGROUNDED_FACT: (
            "State only amounts, numbers, identifiers, dates and order statuses that appear in "
            "the evidence."
        ),
        GateRule.CONTACT_OR_URL: (
            "Do not include a URL, web address, e-mail address or phone number."
        ),
        GateRule.LENGTH: (
            f"Use at most {MAX_SENTENCES} sentences and {EXPLANATION_MAX_CHARS} characters."
        ),
        GateRule.INTERNAL_NAME: (
            "Do not name internal artefacts: event or topic names, upper-case identifiers, class "
            "or field names, code or error traces."
        ),
        GateRule.NOT_PLAIN: "Write plain text without markdown, lists, headings or line breaks.",
        GateRule.ACTION_NOT_ALLOWED: "Choose the suggested action from the allowed actions.",
    }
)
"""The fixed message of each rule; never contains draft or evidence text."""

_NOT_PLAIN: Final = re.compile(r"[\r\n*#|\[\]<>~]|^\s*(?:[-+]|\d+[.)])\s", re.M)
_SENTENCE_END: Final = re.compile(r"[.!?]+(?:\s+|$)")


def _evidence_strings(case: CaseForTriage) -> Iterator[str]:
    """Every text an explanation may draw facts from."""
    yield str(case.order_id)
    for item in case.evidence:
        yield item.topic
        yield item.occurred_at.isoformat()
        if item.reason is not None:
            yield item.reason
        yield from json_leaves(item.payload)


def _evidence(case: CaseForTriage) -> Evidence:
    return Evidence.from_strings(_evidence_strings(case), {item.topic for item in case.evidence})


def _too_long(explanation: str) -> bool:
    sentences = [part for part in _SENTENCE_END.split(explanation) if part.strip()]
    return len(explanation) > EXPLANATION_MAX_CHARS or len(sentences) > MAX_SENTENCES


def _broken_rules(draft: DraftOutput, case: CaseForTriage) -> Iterable[GateRule]:
    explanation = draft.explanation
    if has_promise(explanation):
        yield GateRule.PROMISE
    if not facts_grounded(explanation, _evidence(case)):
        yield GateRule.UNGROUNDED_FACT
    if has_contact_or_url(explanation):
        yield GateRule.CONTACT_OR_URL
    if _too_long(explanation):
        yield GateRule.LENGTH
    if has_internal_name(explanation):
        yield GateRule.INTERNAL_NAME
    if _NOT_PLAIN.search(explanation):
        yield GateRule.NOT_PLAIN
    if draft.suggested_action not in allowed_actions(case.cause):
        yield GateRule.ACTION_NOT_ALLOWED


def check(draft: DraftOutput, case: CaseForTriage, attempt: int = 1) -> list[Violation]:
    """Return the rules ``draft`` breaks for ``case``, once each; empty when it may be used."""
    return [
        Violation(rule=rule, message=RULE_MESSAGES[rule], attempt=attempt)
        for rule in _broken_rules(draft, case)
    ]
