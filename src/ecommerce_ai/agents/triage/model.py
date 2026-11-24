"""Value types of the triage graph: actions, sources, gate rules, cases, drafts and results."""

from enum import StrEnum
from typing import Final, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from ecommerce_ai.agents.triage_intake.model import TriageCause

EXPLANATION_MAX_CHARS: Final = 500
"""Longest explanation this service produces (the contract allows 1000)."""

MAX_REVISIONS: Final = 2
"""Drafts after the first one, at most (FR-005)."""


class SuggestedAction(StrEnum):
    """Advice for a human; same values as the ``suggestedAction`` enum of ``events.md``."""

    CONTACT_CUSTOMER = "CONTACT_CUSTOMER"
    RESTOCK = "RESTOCK"
    FIX_CATALOG = "FIX_CATALOG"
    REVIEW = "REVIEW"
    NONE = "NONE"


class ResultSource(StrEnum):
    """What produced a result; same values as the ``source`` enum of ``events.md``."""

    LLM = "LLM"
    FALLBACK_TEMPLATE = "FALLBACK_TEMPLATE"


class GateRule(StrEnum):
    """Rule codes of the gatekeeper (FR-008)."""

    PROMISE = "PROMISE"
    UNGROUNDED_FACT = "UNGROUNDED_FACT"
    CONTACT_OR_URL = "CONTACT_OR_URL"
    LENGTH = "LENGTH"
    INTERNAL_NAME = "INTERNAL_NAME"
    NOT_PLAIN = "NOT_PLAIN"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"


class EvidenceItem(BaseModel):
    """One stored event of a case: the only source of facts an explanation may state."""

    model_config = ConfigDict(frozen=True)

    topic: str = Field(min_length=1, description="Kafka topic the event came from.")
    reason: str | None = Field(description="The event's reason, if any. Untrusted text.")
    occurred_at: AwareDatetime = Field(description="When the event happened.")
    payload: dict[str, JsonValue] = Field(description="The stored event. Untrusted data.")


class CaseForTriage(BaseModel):
    """A settled case, locked for triage, with all its evidence."""

    model_config = ConfigDict(frozen=True)

    case_id: UUID = Field(description="Id of the case.")
    order_id: UUID = Field(description="The order the case is about.")
    cause: TriageCause = Field(description="Cause stored on the case (FR-006).")
    evidence: tuple[EvidenceItem, ...] = Field(description="Stored events, oldest first.")


class DraftOutput(BaseModel):
    """The structured answer asked from the model (FR-007); never partially used."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    explanation: str = Field(
        min_length=1,
        max_length=EXPLANATION_MAX_CHARS,
        description="Plain-language explanation for an operator, at most 3 sentences.",
    )
    suggested_action: SuggestedAction = Field(description="One of the allowed actions.")
    confidence: float = Field(ge=0, le=1, description="Confidence in the explanation, 0 to 1.")


class Violation(BaseModel):
    """One gatekeeper finding: a rule code and a fixed message, never evidence values."""

    model_config = ConfigDict(frozen=True)

    rule: GateRule = Field(description="The rule that was broken.")
    message: str = Field(min_length=1, description="Fixed text describing the rule.")
    attempt: int = Field(ge=1, description="Draft attempt that broke it (1 = first draft).")


class TriageResult(BaseModel):
    """The outcome of one case, as stored in ``triage_results`` (ids and times come later)."""

    model_config = ConfigDict(frozen=True)

    cause: TriageCause = Field(description="Cause of the case.")
    explanation: str = Field(
        min_length=1, max_length=EXPLANATION_MAX_CHARS, description="Gated explanation."
    )
    suggested_action: SuggestedAction = Field(description="Suggested action for a human.")
    confidence: float = Field(ge=0, le=1, description="Confidence, 0 to 1.")
    source: ResultSource = Field(description="LLM or FALLBACK_TEMPLATE.")
    prompt_version: str = Field(min_length=1, description="Prompt version, e.g. `triage.v1`.")
    model_id: str | None = Field(description="Model id; null exactly when source is a fallback.")
    input_tokens: int = Field(ge=0, description="Input tokens summed over every attempt.")
    output_tokens: int = Field(ge=0, description="Output tokens summed over every attempt.")
    latency_ms: int = Field(ge=0, description="Duration of the whole graph run.")
    revisions: int = Field(ge=0, le=MAX_REVISIONS, description="Drafts after the first one.")
    violations: tuple[Violation, ...] = Field(description="Every gatekeeper finding, in order.")

    @model_validator(mode="after")
    def _model_id_matches_source(self) -> Self:
        """``model_id`` is null if and only if the fallback template produced the result."""
        if (self.model_id is None) != (self.source is ResultSource.FALLBACK_TEMPLATE):
            raise ValueError("model_id must be null exactly when source is FALLBACK_TEMPLATE")
        return self
