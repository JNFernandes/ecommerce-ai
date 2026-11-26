"""Leak checks on answer text (FR-007, FR-008, research §7): pure, deterministic, no I/O.

``LeakChecks.check`` returns one output ``replaced`` event per rule the text breaks; the rule
names are the gatekeeper's ``ChatRule`` values:

- ``canary`` (high): this run's canary, also with separators or the prefix removed;
- ``token_like`` (high): a JWT (``eyJ….….…``) or a bearer token;
- ``secret_like`` (high): ``sk-ant-``/``sk-`` keys, ``AKIA`` access keys, ``ghp_`` tokens, or a
  ``key=``/``secret:``/``password=`` assignment;
- ``foreign_id`` (medium): a UUID that neither the customer nor a tool result of this
  conversation gave (``OutputContext.seen_ids``);
- ``prompt_leak`` (high): any run of ``SHINGLE_WORDS`` consecutive words of the system prompt.

Events never quote the text.
"""

import re
from collections.abc import Iterable, Iterator
from functools import lru_cache
from typing import Final

from ecommerce_ai.agents.guardrails import OutputContext
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)

SHINGLE_WORDS: Final = 12
"""Consecutive system-prompt words that count as a verbatim prompt fragment."""

UUID_PATTERN: Final = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
_JWT: Final = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_BEARER: Final = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{20,}", re.I)
_SECRETS: Final = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(
        r"\b(?:api[_-]?key|secret|password|passwd|access[_-]?key|client[_-]?secret)"
        r"\s*[:=]\s*\S{6,}",
        re.I,
    ),
)
_WORD: Final = re.compile(r"\w+")
_MIN_CANARY_BODY: Final = 16
"""Shortest canary body (without ``cnry``) still matched on its own."""
_NOT_ALNUM: Final = re.compile(r"[^0-9a-z]")

_SEVERITY: Final = {
    "canary": GuardSeverity.HIGH,
    "token_like": GuardSeverity.HIGH,
    "secret_like": GuardSeverity.HIGH,
    "foreign_id": GuardSeverity.MEDIUM,
    "prompt_leak": GuardSeverity.HIGH,
}


def extract_ids(texts: Iterable[str]) -> frozenset[str]:
    """The UUIDs in ``texts``, lower-cased."""
    return frozenset(match.lower() for text in texts for match in UUID_PATTERN.findall(text))


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


@lru_cache(maxsize=8)
def prompt_shingles(system_prompt: str) -> frozenset[tuple[str, ...]]:
    """Every run of ``SHINGLE_WORDS`` consecutive words of ``system_prompt``."""
    words = _words(system_prompt)
    return frozenset(
        tuple(words[index : index + SHINGLE_WORDS])
        for index in range(len(words) - SHINGLE_WORDS + 1)
    )


def _has_canary(text: str, canary: str | None) -> bool:
    if not canary:
        return False
    compact = _NOT_ALNUM.sub("", text.casefold())
    secret = _NOT_ALNUM.sub("", canary.casefold())
    body = secret.removeprefix("cnry")
    return secret in compact or (len(body) >= _MIN_CANARY_BODY and body in compact)


def _leaks_prompt(text: str, system_prompt: str) -> bool:
    if not system_prompt:
        return False
    words = _words(text)
    if len(words) < SHINGLE_WORDS:
        return False
    shingles = prompt_shingles(system_prompt)
    return any(
        tuple(words[index : index + SHINGLE_WORDS]) in shingles
        for index in range(len(words) - SHINGLE_WORDS + 1)
    )


def _broken(text: str, context: OutputContext) -> Iterator[str]:
    if _has_canary(text, context.canary):
        yield "canary"
    if _JWT.search(text) or _BEARER.search(text):
        yield "token_like"
    if any(pattern.search(text) for pattern in _SECRETS):
        yield "secret_like"
    if extract_ids([text]) - context.seen_ids:
        yield "foreign_id"
    if _leaks_prompt(text, context.system_prompt):
        yield "prompt_leak"


class LeakChecks:
    """The ``OutputChecks`` of the guardrails bundle."""

    def check(self, text: str, context: OutputContext) -> tuple[GuardEvent, ...]:
        """One output ``replaced`` event per leak rule ``text`` breaks; empty when clean."""
        return tuple(
            GuardEvent(
                stage=GuardStage.OUTPUT,
                rule=rule,
                action=GuardAction.REPLACED,
                severity=_SEVERITY[rule],
            )
            for rule in _broken(text, context)
        )
