"""Deterministic gatekeeper of every chat answer (FR-015, FR-016, research §9): pure, no I/O.

``check_segment`` runs on each segment before it is sent, ``check_answer`` on the whole answer
before ``message.end``; both return the broken rules (empty = safe) and never quote the text. The
rules are the shared ones of ``agents/safety/rules.py`` with the chat's own allowances: newlines
and ``-`` lists are fine (only HTML counts as not plain), and a status-like word (``SHIPPED``,
``MacBook``) or a past-tense status (``delivered``, ``refunded``) is allowed when the tool data
contains it. Internal names (``ecommerce-*``, topic names, ``graphql``, ``localhost``) are never
allowed, even when a tool returned them.

Evidence (``ChatEvidence.from_transcript``) = the leaf values of the ``data`` of every successful
tool result in the conversation state, plus the ids, amounts, dates, quantities and SKU-like words
the customer wrote (never their status words). Envelope metadata and error bodies are never
evidence. Order statuses (``PENDING``, ``CONFIRMED``, ``CANCELLED``) are public words, never
internal names. ``cancelled`` is not checked as a plain status word: the cancellation-claim rule
decides it, so "cannot be cancelled" or "only pending orders can be cancelled" pass. A
cancellation claim (US-06, FR-012) is allowed only for orders a successful ``cancel_order``,
``get_order`` or ``list_my_orders`` result holds as ``CANCELLED`` (a claim naming no order: any
such order); text inside other data never grounds it. Every
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
from ecommerce_ai.agents.guardrails import NOOP_OUTPUT_CHECKS, OutputChecks, OutputContext
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.guardrails.output_checks import extract_ids
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
)
from ecommerce_ai.agents.triage_intake.cause import ORDER_CANCELLED, TRIAGE_TOPICS

SAFE_MESSAGE: Final = (
    "Sorry, I can't give you a reliable answer to that. "
    "Please ask me about your orders, products or notifications."
)
"""Sent instead of a segment or answer that breaks a rule (finishReason ``replaced``)."""

NO_ANSWER_MESSAGE: Final = (
    "Sorry, I could not find an answer to that. Please try asking in a different way."
)
"""Sent when the model gave no text at all (finishReason ``replaced``)."""

BLOCKED_REFUSAL: Final = (
    "I can't help with that request. I can help with your orders, products and notifications."
)
"""Sent instead of an answer when the input guard blocks the message (finishReason ``blocked``)."""

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
    CANARY = "canary"
    TOKEN_LIKE = "token_like"  # noqa: S105  # a rule name, not a token
    SECRET_LIKE = "secret_like"  # noqa: S105  # a rule name, not a secret
    FOREIGN_ID = "foreign_id"
    PROMPT_LEAK = "prompt_leak"


_HIGH: Final = frozenset(
    {ChatRule.CANARY, ChatRule.TOKEN_LIKE, ChatRule.SECRET_LIKE, ChatRule.PROMPT_LEAK}
)
_MEDIUM: Final = frozenset(
    {ChatRule.FOREIGN_ID, ChatRule.INTERNAL_NAME, ChatRule.CANCELLATION_CLAIM}
)


def rule_severity(rule: ChatRule) -> GuardSeverity:
    """How serious breaking ``rule`` is: leaks are ``high``, data exposure ``medium``."""
    if rule in _HIGH:
        return GuardSeverity.HIGH
    return GuardSeverity.MEDIUM if rule in _MEDIUM else GuardSeverity.LOW


def replaced_events(rules: Iterable[str]) -> tuple[GuardEvent, ...]:
    """One output ``replaced`` event per distinct broken rule (US-07 FR-019)."""
    return tuple(
        GuardEvent(
            stage=GuardStage.OUTPUT,
            rule=rule,
            action=GuardAction.REPLACED,
            severity=rule_severity(ChatRule(rule)),
        )
        for rule in dict.fromkeys(rules)
    )


INTERNAL_TABLES: Final = frozenset(
    {
        "alembic_version",
        "chat_actions",
        "chat_approvals",
        "chat_checkpoint_writes",
        "chat_checkpoints",
        "chat_conversations",
        "chat_messages",
        "chat_user_usage",
        "guardrail_events",
        "inbound_dead_letters",
        "llm_calls",
        "llm_token_usage",
        "outbox_events",
        "triage_cases",
        "triage_evidence",
        "triage_results",
    }
)
"""The service's table names (``infrastructure/db/tables.py``; a test keeps them equal)."""


_ALWAYS_INTERNAL: Final = re.compile(
    r"\becommerce-[a-z0-9-]*|\bgraphql\b|\blocalhost\b|"
    r"\b[a-z0-9.-]+:5436\b|\bai:8002\b|\bpostgres-ai\b|"
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.internal\b|"
    + "|".join(rf"\b{re.escape(name)}\b" for name in sorted(INTERNAL_TABLES))
    + "|"
    + "|".join(re.escape(topic) for topic in TRIAGE_TOPICS),
    re.I,
)
_WORD: Final = re.compile(r"[A-Za-z0-9]+")
_ORDER_TOOLS: Final = frozenset({CANCEL_ACTION, READ_TOOL, "list_my_orders"})
"""Tools whose data may show an order ``CANCELLED`` (FR-012)."""
_CANCELLED: Final = "CANCELLED"
_ORDER_STATUSES: Final = frozenset({"PENDING", "CONFIRMED", _CANCELLED})
"""Public order statuses: never internal names (``tools/orders.py::OrderStatus``)."""
_CLAIM_RULED: Final = frozenset({ORDER_CANCELLED})
"""Status topics whose words the dedicated cancellation-claim rule checks instead."""


class ChatEvidence:
    """What an answer may state: facts of the tool data and the customer's ids."""

    def __init__(
        self,
        data: Iterable[str],
        customer_texts: Iterable[str],
        cancelled: Iterable[str | None] = (),
        *,
        approval_resolved: bool = False,
        output_checks: OutputChecks = NOOP_OUTPUT_CHECKS,
        output_context: OutputContext | None = None,
    ) -> None:
        """Index the tool ``data`` leaves and the facts of the customer's own messages.

        ``cancelled`` holds the id of each order the order tools showed ``CANCELLED`` (``None``
        when the order had no id); ``approval_resolved`` marks a run resumed from a decision.
        ``output_checks`` (US-07 leak checks) compare the text with ``output_context``.
        """
        self.output_checks = output_checks
        self.output_context = output_context or OutputContext()
        strings = list(data)
        customer = list(customer_texts)
        self.facts = Evidence.from_strings(strings, _CLAIM_RULED, customer)
        self.lower_words = {word.lower() for text in strings for word in _WORD.findall(text)}
        self.words = {
            word for text in [*strings, *customer] for word in _WORD.findall(text)
        } | _ORDER_STATUSES
        orders = list(cancelled)
        self.any_cancelled = bool(orders)
        self.cancelled_ids = frozenset(order for order in orders if order is not None)
        self.approval_resolved = approval_resolved

    @classmethod
    def from_transcript(
        cls,
        transcript: Iterable[TranscriptItem],
        *,
        approval_resolved: bool = False,
        output_checks: OutputChecks = NOOP_OUTPUT_CHECKS,
        canary: str | None = None,
        system_prompt: str = "",
    ) -> Self:
        """Evidence of every trusted tool result and every customer message of the state.

        The leak checks also get the run's ``canary``, its rendered ``system_prompt`` and the ids
        seen in the conversation (``seen_ids``).
        """
        items = list(transcript)
        return cls(
            (leaf for item in items for leaf in _data_leaves(item)),
            (item.text for item in items if isinstance(item, UserTurn)),
            (order for item in items for order in _cancelled_orders(item)),
            approval_resolved=approval_resolved,
            output_checks=output_checks,
            output_context=OutputContext(
                canary=canary, seen_ids=seen_ids(items), system_prompt=system_prompt
            ),
        )


def seen_ids(transcript: Iterable[TranscriptItem]) -> frozenset[str]:
    """The UUIDs the customer typed or any tool result returned in the conversation (FR-008)."""
    texts: list[str] = []
    for item in transcript:
        if isinstance(item, UserTurn):
            texts.append(item.text)
        elif isinstance(item, ToolResultItem):
            texts.extend(json_leaves(item.payload))
    return extract_ids(texts)


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
    for event in evidence.output_checks.check(text, evidence.output_context):
        yield ChatRule(event.rule)


def check_segment(text: str, evidence: ChatEvidence) -> tuple[ChatRule, ...]:
    """The rules one segment breaks, once each; empty when it may be sent."""
    return tuple(dict.fromkeys(_broken_rules(text, evidence)))


def check_answer(text: str, evidence: ChatEvidence) -> tuple[ChatRule, ...]:
    """The rules the whole answer breaks (a rule may span segments); empty when it is safe."""
    return tuple(dict.fromkeys(_broken_rules(text, evidence)))
