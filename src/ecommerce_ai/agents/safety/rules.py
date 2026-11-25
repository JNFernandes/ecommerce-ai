"""Shared gatekeeper rules (research §9): pure functions over a text and its evidence.

Facts (amounts and numbers, UUIDs, ISO and written dates, order status words) must be found in the
``Evidence``. Ids and ISO dates are removed before the contact and internal-name checks, so their
digits and letters never look like phone numbers or identifiers. Nothing here quotes the text it
checks: callers turn a broken rule into their own fixed message. A cancellation claim (US-06) must
name only orders the evidence holds as ``CANCELLED`` (``cancellation_claims_grounded``).
"""

import re
from collections.abc import Collection, Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Self

from pydantic import JsonValue

from ecommerce_ai.agents.triage_intake.cause import (
    ORDER_CANCELLED,
    PAYMENT_FAILED,
    STOCK_REJECTED,
)

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

_HTML_TAG: Final = re.compile(r"</?[a-z][a-z0-9-]*(?:\s[^<>]*)?/?>|<!--", re.I)


def json_leaves(value: JsonValue) -> Iterator[str]:
    """Leaf values of a JSON value, as text (keys are not facts; booleans and nulls are skipped)."""
    if isinstance(value, dict):
        for child in value.values():
            yield from json_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_leaves(child)
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


def scrub_ids_and_dates(text: str) -> str:
    """``text`` with UUIDs and ISO dates blanked out, for checks that must not see them."""
    return _strip(_ISO_DATE, _strip(_UUID, text))


class Evidence:
    """Facts a text may state: UUIDs, ISO dates, numbers and status words of the source texts.

    The parts of every ISO date and time (year, month, day, hour, minute, second) are numbers of
    the evidence too, so a date or time written another way (``24/11/2026``, ``10:15``) is
    grounded by the timestamp it comes from.
    """

    def __init__(self, text: str, topics: frozenset[str], facts: str = "") -> None:
        """Index the facts of ``text`` and ``facts``; ``topics`` ground status words.

        Status words (``cancelled``, ``shipped``...) are grounded only by ``text`` or ``topics``;
        ``facts`` (e.g. the customer's own messages) adds only ids, dates and numbers.
        """
        self.text = text
        self.topics = topics
        source = f"{text}\n{facts}" if facts else text
        self.uuids = {match.group().lower() for match in _UUID.finditer(source)}
        without_uuids = _strip(_UUID, source)
        self.dates = _iso_dates(without_uuids)
        self.numbers = {
            number
            for match in _NUMBER.finditer(_strip(_ISO_DATE, without_uuids))
            if (number := _number(match.group())) is not None
        } | {
            number
            for match in _ISO_DATE.finditer(without_uuids)
            for part in re.findall(r"\d+", match.group())
            if (number := _number(part)) is not None
        }

    @classmethod
    def from_strings(
        cls, strings: Iterable[str], topics: Iterable[str] = (), facts: Iterable[str] = ()
    ) -> Self:
        """Evidence of ``strings`` (e.g. JSON leaves), their ``topics`` and extra ``facts``."""
        return cls("\n".join(strings), frozenset(topics), "\n".join(facts))

    def has_date(self, month: int, day: int | None, year: int | None) -> bool:
        """True when a date of the evidence matches every given part."""
        return any(
            found.month == month
            and (day is None or found.day == day)
            and (year is None or found.year == year)
            for found in self.dates
        )


def _written_date_grounded(match: re.Match[str], evidence: Evidence) -> bool:
    month_name = (match["month1"] or match["month2"] or match["month3"]).lower()
    day_text = match["day1"] or match["day2"]
    day = int(re.sub(r"\D", "", day_text)) if day_text else None
    year = int(match["year"]) if match["year"] else None
    return evidence.has_date(_MONTHS[month_name], day, year)


def facts_grounded(text: str, evidence: Evidence) -> bool:
    """True when every UUID, date, number and status word of ``text`` appears in ``evidence``."""
    if any(match.group().lower() not in evidence.uuids for match in _UUID.finditer(text)):
        return False
    rest = _strip(_UUID, text)
    if not _iso_dates(rest) <= evidence.dates:
        return False
    rest = _strip(_ISO_DATE, rest)
    if any(not _written_date_grounded(match, evidence) for match in _WRITTEN_DATE.finditer(rest)):
        return False
    rest = _strip(_WRITTEN_DATE, rest)
    if any(_number(match.group()) not in evidence.numbers for match in _NUMBER.finditer(rest)):
        return False
    return all(
        (topic is not None and topic in evidence.topics) or pattern.search(evidence.text)
        for pattern, topic in _STATUS_WORDS
        if pattern.search(text)
    )


def uuids_in(text: str) -> list[str]:
    """Every UUID of ``text``, lower-cased, in order."""
    return [match.group().lower() for match in _UUID.finditer(text)]


def promise_matches(text: str) -> list[str]:
    """Every promise word of ``text`` (see ``has_promise``).

    Returned so a gatekeeper can allow some of them (e.g. ``delivered`` as a grounded status).
    """
    return [match.group() for match in _PROMISE.finditer(text)]


def has_promise(text: str) -> bool:
    """True when ``text`` promises or announces a refund, compensation or any future action."""
    return _PROMISE.search(text) is not None


def has_contact_or_url(text: str) -> bool:
    """True when ``text`` holds a URL, web address, e-mail address or phone number."""
    return _CONTACT.search(scrub_ids_and_dates(text)) is not None


def internal_name_matches(text: str) -> list[str]:
    """Every internal-looking name of ``text``: code, traces, topics, snake/camel/upper case.

    Returned so a gatekeeper can allow some of them (e.g. order statuses found in the evidence).
    """
    return [match.group() for match in _INTERNAL.finditer(scrub_ids_and_dates(text))]


def has_internal_name(text: str) -> bool:
    """True when ``text`` names an internal artefact (see ``internal_name_matches``)."""
    return _INTERNAL.search(scrub_ids_and_dates(text)) is not None


def has_html(text: str) -> bool:
    """True when ``text`` holds an HTML tag or comment."""
    return _HTML_TAG.search(text) is not None


_CANCELLATION_CLAIM: Final = re.compile(
    r"\bcancel(?:l)?ed\b"
    r"|\bwill\s+(?:now\s+)?(?:be\s+)?cancel\w*"
    r"|\bcancellation\s+(?:is|was|has\s+been)\s+(?:done|complete\w*|successful|confirmed|processed)"
    r"|\bcancelad[oa]s?\b|\bcancel(?:ei|ou|amos|aram)\b"
    r"|\b(?:vou|vamos|irei|iremos|vai|v[aã]o)\s+(?:ser\s+)?cancel\w*"
    r"|\bcancelamento\s+(?:foi|est[aá])\s+(?:feito|conclu[ií]do|efetuado|confirmado|realizado)",
    re.I,
)
_NEGATED: Final = re.compile(
    r"(?:\bnot|\bcannot|n't|\bnever|\bn[aã]o|\bnunca)\s+(?:\w+\s+){0,2}$", re.I
)
_MODAL: Final = re.compile(
    r"\b(?:can|could|may|might|must|should|cannot)(?:\s+not|n't)?\s+(?:\w+\s+)?be\s+$", re.I
)
"""A possibility, not a claim: "only pending orders can be cancelled"."""
_SENTENCE: Final = re.compile(r"[^.!?\n]+[.!?]*")


def cancellation_claims(text: str) -> Iterator[tuple[str, ...]]:
    """For each sentence of ``text`` saying an order is or will be cancelled: the ids it names.

    English and Portuguese; questions, negated forms ("was not cancelled") and possibilities
    ("can be cancelled") are no claims. An empty tuple is a claim that names no order.
    """
    for sentence in _SENTENCE.finditer(text):
        part = sentence.group()
        if part.rstrip().endswith("?"):
            continue
        claimed = any(
            not _NEGATED.search(part[: match.start()]) and not _MODAL.search(part[: match.start()])
            for match in _CANCELLATION_CLAIM.finditer(part)
        )
        if claimed:
            yield tuple(uuids_in(part))


_AWAITING_APPROVAL: Final = re.compile(
    r"\b(?:approve|reject|confirm)\s+(?:or\s+(?:approve|reject)\s+)?(?:the|this|that)\s+"
    r"(?:request|action|cancell?ation)"
    r"|\b(?:awaiting|pending|waiting\s+for)\s+(?:your\s+)?(?:approval|confirmation|decision)"
    r"|\bsent\s+(?:a|the)\s+cancell?ation\s+request"
    r"|\bplease\s+(?:approve|confirm|reject)\b"
    r"|\baguard\w*\s+(?:a\s+sua\s+)?(?:aprova|confirma)\w*"
    r"|\b(?:aprove|aprovar|rejeite|rejeitar|confirme)\s+(?:o|a|este|esta)\s+(?:pedido|a[cç][aã]o|cancelamento)",
    re.I,
)


def has_awaiting_approval_language(text: str) -> bool:
    """True when ``text`` asks for, or waits on, an approval (EN/PT), e.g. "please approve"."""
    return _AWAITING_APPROVAL.search(text) is not None


def cancellation_claims_grounded(
    text: str, cancelled_ids: Collection[str], *, any_cancelled: bool
) -> bool:
    """True when every cancellation claim of ``text`` is backed by a ``CANCELLED`` order (FR-012).

    A claim naming ids needs each of them in ``cancelled_ids`` (lower-case); a claim naming none
    needs ``any_cancelled``.
    """
    return all(
        set(ids) <= set(cancelled_ids) if ids else any_cancelled
        for ids in cancellation_claims(text)
    )
