"""Deterministic input rules from a versioned data file (FR-002, FR-003, research §3).

``rules/input.<version>.json`` holds ``{"version", "rules": [{name, category, severity,
patterns}]}``. ``parse_input_rules`` validates it (unique names, known categories and severities,
every pattern a valid regex) and compiles each pattern once; an invalid file raises
``ValueError``, so the app build fails. Adding a rule is a data change, never a code branch.

Patterns are written in lower case without accents: ``RuleInputGuard`` matches them against every
``match_variants`` form of the normalised message (casefolded, de-leeted, separator-squashed,
decoded) and the accent-folded copy of each, so ``Ignorez les règles``, ``i.g.n.o.r.e`` and a
base64 run hit the same rule as the plain English attack. The first rule (file order) that
matches wins; its event never quotes the message.
"""

import re
import unicodedata
from enum import StrEnum
from functools import cached_property
from importlib.resources import files
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.guardrails.normalizer import match_variants

INPUT_RULES_VERSION: Final = "v1"
"""The active rules file, ``rules/input.<INPUT_RULES_VERSION>.json``."""

_PACKAGE: Final = "ecommerce_ai.agents.guardrails.rules"


class RuleCategory(StrEnum):
    """What kind of attack a rule detects (FR-002)."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLEPLAY_JAILBREAK = "roleplay_jailbreak"
    EXTRACTION = "extraction"
    IMPERSONATION = "impersonation"
    APPROVAL_ON_BEHALF = "approval_on_behalf"
    OTHER_CUSTOMERS_DATA = "other_customers_data"
    OBFUSCATION = "obfuscation"


class InputRule(BaseModel):
    """One named rule: any of its patterns matching a variant breaks it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", description="Recorded as the rule.")
    category: RuleCategory
    severity: GuardSeverity
    patterns: tuple[str, ...] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _valid_regexes(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid pattern {pattern!r}: {error}") from error
        return patterns

    @cached_property
    def compiled(self) -> tuple[re.Pattern[str], ...]:
        """The patterns, compiled once (``.`` also matches a newline)."""
        return tuple(re.compile(pattern, re.DOTALL) for pattern in self.patterns)

    def matches(self, variants: tuple[str, ...]) -> bool:
        """Whether any pattern finds a match in any variant."""
        return any(pattern.search(text) for pattern in self.compiled for text in variants)


class InputRulesFile(BaseModel):
    """The validated rules file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^v[0-9]+$")
    rules: tuple[InputRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [rule.name for rule in self.rules]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate rule names: {duplicates}")
        return self


def parse_input_rules(text: str) -> InputRulesFile:
    """Validate a rules file's JSON text; ``ValueError`` when anything in it is wrong."""
    return InputRulesFile.model_validate_json(text)


def input_rules_text(version: str = INPUT_RULES_VERSION) -> str:
    """The packaged ``rules/input.<version>.json`` (``FileNotFoundError`` if absent)."""
    return files(_PACKAGE).joinpath(f"input.{version}.json").read_text(encoding="utf-8")


def load_input_rules(version: str = INPUT_RULES_VERSION) -> InputRulesFile:
    """Load and validate the packaged rules file of ``version``."""
    return parse_input_rules(input_rules_text(version))


def _fold_accents(text: str) -> str:
    """``text`` without combining marks (``é`` → ``e``, ``ü`` → ``u``)."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def rule_variants(text: str) -> tuple[str, ...]:
    """Every form of ``text`` the rules are matched against."""
    variants = match_variants(text)
    return tuple(dict.fromkeys([*variants, *(_fold_accents(v) for v in variants)]))


class RuleInputGuard:
    """The ``InputGuard`` over a rules file: a message breaking a rule is ``blocked``."""

    def __init__(self, rules: InputRulesFile) -> None:
        """Match against ``rules``; their patterns are compiled here, once."""
        self._rules = rules
        for rule in rules.rules:
            _ = rule.compiled

    @property
    def version(self) -> str:
        """Version of the rules file, reported as ``rules_version`` on ``chat_run``."""
        return self._rules.version

    def first_match(self, text: str) -> InputRule | None:
        """The first rule (file order) that ``text`` breaks, ``None`` when it breaks none."""
        variants = rule_variants(text)
        return next((rule for rule in self._rules.rules if rule.matches(variants)), None)

    def check(self, text: str) -> GuardEvent | None:
        """The input ``blocked`` event of the first rule ``text`` breaks, ``None`` if it passes."""
        rule = self.first_match(text)
        if rule is None:
            return None
        return GuardEvent(
            stage=GuardStage.INPUT,
            rule=rule.name,
            action=GuardAction.BLOCKED,
            severity=rule.severity,
        )
