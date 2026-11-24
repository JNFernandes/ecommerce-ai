"""Deterministic gatekeeper of every draft (FR-008, research 8): pure functions, no I/O.

``check`` returns one ``Violation`` per broken rule, each with a fixed message that never quotes
the draft or the evidence. Facts (amounts and numbers, UUIDs, ISO and written dates, order status
words) must be found in the case's evidence: payload values, reasons, times and topics.
"""

import re
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final

from pydantic import JsonValue

from ecommerce_ai.agents.triage.model import (
    EXPLANATION_MAX_CHARS,
    CaseForTriage,
    DraftOutput,
    GateRule,
    Violation,
)
from ecommerce_ai.agents.triage.policy import allowed_actions
from ecommerce_ai.agents.triage_intake.cause import (
    ORDER_CANCELLED,
    PAYMENT_FAILED,
    STOCK_REJECTED,
)

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

_MONTHS: Final = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH: Final = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_DAY: Final = r"\d{1,2}(?:st|nd|rd|th)?"

_UUID: Final = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_ISO_DATE: Final = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
    re.I,
)
_WRITTEN_DATE: Final = re.compile(
    rf"\b(?:(?P<day1>{_DAY})\s+(?:of\s+)?(?P<month1>{_MONTH})\.?"
    rf"|(?P<month2>{_MONTH})\.?\s+(?P<day2>{_DAY})(?!\d)"
    rf"|(?P<month3>{_MONTH})\.?(?=,?\s+\d{{4}}\b))"
    r"(?:,?\s+(?P<year>\d{4}))?\b",
    re.I,
)
_NUMBER: Final = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?!\w)")

_STATUS_WORDS: Final = (
    (re.compile(r"\bcancel(?:l?ed|lation)\b", re.I), ORDER_CANCELLED),
    (
        re.compile(r"\bpayment\s+(?:has\s+|had\s+|was\s+)?(?:failed|refused)\b", re.I),
        PAYMENT_FAILED,
    ),
    (re.compile(r"\b(?:out\s+of\s+stock|insufficient\s+stock)\b", re.I), STOCK_REJECTED),
    (re.compile(r"\b(?:shipped|dispatched|delivered|refunded|paid|confirmed)\b", re.I), None),
)

_PROMISE: Final = re.compile(
    r"\b(?:refund\w*|reimburs\w*|compensat\w*|voucher\w*|coupon\w*|discount\w*|guarantee\w*"
    r"|promis\w*|deliver\w*|free\s+of\s+charge|will|shall|going\s+to|credit(?!\s+card)\w*)\b"
    r"|\b\w+'ll\b",
    re.I,
)

_CONTACT: Final = re.compile(
    r"https?://|\bwww\.|[\w.+-]+@[\w-]+\.\w+"
    r"|\b[\w-]+\.(?:com|net|org|io|co|eu|de|pt|uk|info|biz|shop|store|app|dev)\b"
    r"|\+\d[\d\s().-]{6,}\d|\(\d{2,4}\)\s?\d|\b\d{3}[\s.-]\d{3}[\s.-]\d{3,4}\b",
    re.I,
)

_INTERNAL: Final = re.compile(
    r"[{}`]|\bTraceback\b|\b\w+(?:Error|Exception)\b"
    r"|\b[a-z]{2,}\.[a-z][a-z-]+\b"  # dotted names such as topics
    r"|\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"  # snake_case and SCREAMING_CASE
    r"|\b[A-Z]{4,}\b"  # upper-case identifiers such as enum values
    r"|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b"  # camelCase and CamelCase
)

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
        yield from _values(item.payload)


def _values(value: JsonValue) -> Iterator[str]:
    """Leaf values of a JSON value, as text (keys are not facts)."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _values(child)
    elif isinstance(value, str):
        yield value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)


def _number(text: str) -> str | None:
    """Canonical form of a number (``49.90`` and ``49.9`` are equal), ``None`` if invalid."""
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the regex only finds valid numbers
        return None
    return format(value.normalize(), "f")


def _iso_dates(text: str) -> set[date]:
    found = set()
    for match in _ISO_DATE.finditer(text):
        try:
            found.add(date(int(match[1]), int(match[2]), int(match[3])))
        except ValueError:
            continue
    return found


def _strip(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(" ", text)


class _Evidence:
    """Facts found in a case's evidence."""

    def __init__(self, case: CaseForTriage) -> None:
        strings = list(_evidence_strings(case))
        self.text = "\n".join(strings)
        self.topics = {item.topic for item in case.evidence}
        self.uuids = {match.group().lower() for match in _UUID.finditer(self.text)}
        without_uuids = _strip(_UUID, self.text)
        self.dates = _iso_dates(without_uuids)
        self.numbers = {
            number
            for match in _NUMBER.finditer(_strip(_ISO_DATE, without_uuids))
            if (number := _number(match.group())) is not None
        }

    def has_date(self, month: int, day: int | None, year: int | None) -> bool:
        return any(
            found.month == month
            and (day is None or found.day == day)
            and (year is None or found.year == year)
            for found in self.dates
        )


def _written_date_grounded(match: re.Match[str], evidence: _Evidence) -> bool:
    month_name = (match["month1"] or match["month2"] or match["month3"]).lower()
    day_text = match["day1"] or match["day2"]
    day = int(re.sub(r"\D", "", day_text)) if day_text else None
    year = int(match["year"]) if match["year"] else None
    return evidence.has_date(_MONTHS[month_name], day, year)


def _facts_grounded(explanation: str, evidence: _Evidence) -> bool:
    """True when every fact of ``explanation`` appears in the evidence."""
    if any(match.group().lower() not in evidence.uuids for match in _UUID.finditer(explanation)):
        return False
    text = _strip(_UUID, explanation)
    if not _iso_dates(text) <= evidence.dates:
        return False
    text = _strip(_ISO_DATE, text)
    if any(not _written_date_grounded(match, evidence) for match in _WRITTEN_DATE.finditer(text)):
        return False
    text = _strip(_WRITTEN_DATE, text)
    if any(_number(match.group()) not in evidence.numbers for match in _NUMBER.finditer(text)):
        return False
    return all(
        (topic is not None and topic in evidence.topics) or pattern.search(evidence.text)
        for pattern, topic in _STATUS_WORDS
        if pattern.search(explanation)
    )


def _too_long(explanation: str) -> bool:
    sentences = [part for part in _SENTENCE_END.split(explanation) if part.strip()]
    return len(explanation) > EXPLANATION_MAX_CHARS or len(sentences) > MAX_SENTENCES


def _broken_rules(draft: DraftOutput, case: CaseForTriage) -> Iterable[GateRule]:
    explanation = draft.explanation
    # Ids and ISO dates are checked as facts; removing them keeps their digits and letters from
    # looking like phone numbers or identifiers.
    scrubbed = _strip(_ISO_DATE, _strip(_UUID, explanation))
    if _PROMISE.search(explanation):
        yield GateRule.PROMISE
    if not _facts_grounded(explanation, _Evidence(case)):
        yield GateRule.UNGROUNDED_FACT
    if _CONTACT.search(scrubbed):
        yield GateRule.CONTACT_OR_URL
    if _too_long(explanation):
        yield GateRule.LENGTH
    if _INTERNAL.search(scrubbed):
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
