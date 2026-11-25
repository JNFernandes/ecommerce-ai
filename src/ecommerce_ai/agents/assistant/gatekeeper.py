"""Deterministic gatekeeper of every chat answer (FR-015, FR-016, research §9): pure, no I/O.

``check_segment`` runs on each segment before it is sent, ``check_answer`` on the whole answer
before ``message.end``; both return the broken rules (empty = safe) and never quote the text. The
rules are the shared ones of ``agents/safety/rules.py`` with the chat's own allowances: newlines
and ``-`` lists are fine (only HTML counts as not plain), and a status-like word (``SHIPPED``,
``MacBook``) or a past-tense status (``delivered``, ``refunded``) is allowed when the tool data
contains it. Internal names (``ecommerce-*``, topic names, ``graphql``, ``localhost``) are never
allowed, even when a tool returned them.

Evidence (``ChatEvidence.from_transcript``) = the leaf values of the ``data`` of every successful
tool result in the conversation state, plus the UUIDs the customer wrote. Envelope metadata and
error bodies are never evidence. A cancellation claim (US-06, FR-012) is allowed only for orders a
successful ``cancel_order``, ``get_order`` or ``list_my_orders`` result holds as ``CANCELLED`` (a
claim naming no order: any such order); text inside other data never grounds it. Every
``cancel_order`` result is built by the service (fixed refusals carry the fresh read's
``{orderId, status}``), so its ``data`` is evidence even when the call was refused. Once an approval
was resolved in the run (``approval_resolved``), asking for or waiting on an approval breaks
``APPROVAL_LANGUAGE``: the answer must be about the outcome.
"""

import re
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Final, Self

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.approvals import CANCEL_ACTION, READ_TOOL
from ecommerce_ai.agents.assistant.model import ToolResultItem, TranscriptItem, UserTurn
from ecommerce_ai.agents.safety.rules import (
    Evidence,
    cancellation_claims_grounded,
    facts_grounded,
    has_awaiting_approval_language,
    has_contact_or_url,
    has_html,
    internal_name_matches,
    json_leaves,
    promise_matches,
    uuids_in,
)
from ecommerce_ai.agents.triage_intake.cause import TRIAGE_TOPICS

SAFE_MESSAGE: Final = (
    "Sorry, I can't give you a reliable answer to that. "
    "Please ask me about your orders, products or notifications."
)
"""Sent instead of a segment or answer that breaks a rule (finishReason ``replaced``)."""

NO_ANSWER_MESSAGE: Final = (
    "Sorry, I could not find an answer to that. Please try asking in a different way."
)
"""Sent when the model gave no text at all (finishReason ``replaced``)."""

TOOL_LIMIT_MESSAGE: Final = (
    "Sorry, I could not finish looking that up. Please ask a simpler question."
)
"""Sent when the model keeps asking for tools past the round cap (finishReason ``tool_limit``)."""


class ChatRule(StrEnum):
    """A rule a chat answer can break."""

    PROMISE = "promise"
    UNGROUNDED_FACT = "ungrounded_fact"
    CONTACT_OR_URL = "contact_or_url"
    INTERNAL_NAME = "internal_name"
    HTML = "html"
    CANCELLATION_CLAIM = "cancellation_claim"
    APPROVAL_LANGUAGE = "approval_language"


_ALWAYS_INTERNAL: Final = re.compile(
    r"\becommerce-[a-z0-9-]*|\bgraphql\b|\blocalhost\b|"
    + "|".join(re.escape(topic) for topic in TRIAGE_TOPICS),
    re.I,
)
_WORD: Final = re.compile(r"[A-Za-z0-9]+")
_ORDER_TOOLS: Final = frozenset({CANCEL_ACTION, READ_TOOL, "list_my_orders"})
"""Tools whose data may show an order ``CANCELLED`` (FR-012)."""
_CANCELLED: Final = "CANCELLED"


class ChatEvidence:
    """What an answer may state: facts of the tool data and the customer's ids."""

    def __init__(
        self,
        data: Iterable[str],
        customer_ids: Iterable[str],
        cancelled: Iterable[str | None] = (),
        *,
        approval_resolved: bool = False,
    ) -> None:
        """Index the tool ``data`` leaves and the UUIDs the customer wrote.

        ``cancelled`` holds the id of each order the order tools showed ``CANCELLED`` (``None``
        when the order had no id); ``approval_resolved`` marks a run resumed from a decision.
        """
        strings = list(data)
        self.facts = Evidence.from_strings([*strings, *customer_ids])
        self.words = {word for text in strings for word in _WORD.findall(text)}
        self.lower_words = {word.lower() for word in self.words}
        orders = list(cancelled)
        self.any_cancelled = bool(orders)
        self.cancelled_ids = frozenset(order for order in orders if order is not None)
        self.approval_resolved = approval_resolved

    @classmethod
    def from_transcript(
        cls, transcript: Iterable[TranscriptItem], *, approval_resolved: bool = False
    ) -> Self:
        """Evidence of every trusted tool result and every customer message of the state."""
        items = list(transcript)
        return cls(
            (leaf for item in items for leaf in _data_leaves(item)),
            (uuid for item in items if isinstance(item, UserTurn) for uuid in uuids_in(item.text)),
            (order for item in items for order in _cancelled_orders(item)),
            approval_resolved=approval_resolved,
        )


def _data_leaves(item: TranscriptItem) -> Iterator[str]:
    if isinstance(item, ToolResultItem) and _trusted(item):
        yield from json_leaves(item.payload["data"])


def _trusted(item: TranscriptItem) -> bool:
    """A result whose ``data`` is evidence: a successful call, or any ``cancel_order`` result."""
    return (
        isinstance(item, ToolResultItem)
        and (item.ok or item.name == CANCEL_ACTION)
        and "data" in item.payload
    )


def _cancelled_orders(item: TranscriptItem) -> Iterator[str | None]:
    """The ids of the orders a trusted order tool result shows ``CANCELLED``."""
    if isinstance(item, ToolResultItem) and item.name in _ORDER_TOOLS and _trusted(item):
        yield from _cancelled_in(item.payload["data"])


def _cancelled_in(value: JsonValue) -> Iterator[str | None]:
    if isinstance(value, list):
        for child in value:
            yield from _cancelled_in(child)
    elif isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, str) and status.upper() == _CANCELLED:
            order_id = value.get("orderId", value.get("id"))
            yield order_id.lower() if isinstance(order_id, str) else None
        for child in value.values():
            yield from _cancelled_in(child)


def _promise_allowed(word: str, evidence: ChatEvidence) -> bool:
    """A past-tense status word (``delivered``) the tool data holds is a fact, not a promise."""
    return word.lower().endswith("ed") and word.lower() in evidence.lower_words


def _name_allowed(name: str, evidence: ChatEvidence) -> bool:
    """A plain word the tool data holds as is (a status, a product name) may be repeated."""
    return _WORD.fullmatch(name) is not None and name in evidence.words


def _broken_rules(text: str, evidence: ChatEvidence) -> Iterator[ChatRule]:
    if any(not _promise_allowed(word, evidence) for word in promise_matches(text)):
        yield ChatRule.PROMISE
    if not facts_grounded(text, evidence.facts):
        yield ChatRule.UNGROUNDED_FACT
    if has_contact_or_url(text):
        yield ChatRule.CONTACT_OR_URL
    if _ALWAYS_INTERNAL.search(text) or any(
        not _name_allowed(name, evidence) for name in internal_name_matches(text)
    ):
        yield ChatRule.INTERNAL_NAME
    if has_html(text):
        yield ChatRule.HTML
    if not cancellation_claims_grounded(
        text, evidence.cancelled_ids, any_cancelled=evidence.any_cancelled
    ):
        yield ChatRule.CANCELLATION_CLAIM
    if evidence.approval_resolved and has_awaiting_approval_language(text):
        yield ChatRule.APPROVAL_LANGUAGE


def check_segment(text: str, evidence: ChatEvidence) -> tuple[ChatRule, ...]:
    """The rules one segment breaks, once each; empty when it may be sent."""
    return tuple(_broken_rules(text, evidence))


def check_answer(text: str, evidence: ChatEvidence) -> tuple[ChatRule, ...]:
    """The rules the whole answer breaks (a rule may span segments); empty when it is safe."""
    return tuple(_broken_rules(text, evidence))
