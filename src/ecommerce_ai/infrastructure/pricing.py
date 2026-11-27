"""Per-model token prices (``config/model_prices.json``, plan D10) and the per-call cost estimate.

The file is read once at startup; a missing or invalid file stops the service (``PriceFileError``),
so a wrong price never goes unnoticed. Costs are ``Decimal`` USD rounded to six places (the
``llm_calls.estimated_cost_usd`` scale). A model the file does not list costs ``None`` and is
warned about once per model id; a dated id (``<listed id>-<date>``) uses its listed id's price.
"""

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

_logger = structlog.get_logger(__name__)

_PER_MTOK: Final = Decimal(1_000_000)
COST_QUANTUM: Final = Decimal("0.000001")
"""Scale of a stored cost estimate (``numeric(12, 6)``)."""


class PriceFileError(Exception):
    """The price file is missing or invalid; the message names the path, never its content."""


class ModelPrice(BaseModel):
    """USD price of one model per million tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: Decimal = Field(ge=0, description="USD per million input tokens.")
    output_per_mtok: Decimal = Field(ge=0, description="USD per million output tokens.")


class ModelPrices(BaseModel):
    """The price file: a version and the price of every model in use."""

    model_config = ConfigDict(frozen=True)

    version: int = Field(ge=1, description="Version of the price list.")
    currency: Literal["USD"] = Field(default="USD", description="Currency of every price.")
    source: str | None = Field(default=None, description="Where and when the prices were taken.")
    models: dict[str, ModelPrice] = Field(description="Prices by model id.")
    _warned: set[str] = PrivateAttr(default_factory=set)

    def price_of(self, model_id: str) -> ModelPrice | None:
        """The price of ``model_id``, or of the listed id it is a dated version of."""
        exact = self.models.get(model_id)
        if exact is not None:
            return exact
        prefixes = [listed for listed in self.models if model_id.startswith(f"{listed}-")]
        return self.models[max(prefixes, key=len)] if prefixes else None

    def estimate_cost(
        self, model_id: str | None, input_tokens: int, output_tokens: int
    ) -> Decimal | None:
        """USD cost of one call; ``None`` without a model or for a model the file does not list."""
        if model_id is None:
            return None
        price = self.price_of(model_id)
        if price is None:
            if model_id not in self._warned:
                self._warned.add(model_id)
                _logger.warning("model_price_unknown", model_id=model_id[:64])
            return None
        cost = (
            Decimal(input_tokens) * price.input_per_mtok
            + Decimal(output_tokens) * price.output_per_mtok
        ) / _PER_MTOK
        return cost.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def load_model_prices(path: str | Path) -> ModelPrices:
    """Read and validate the price file at ``path``; ``PriceFileError`` when it cannot be used."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        reason = type(error).__name__
        raise PriceFileError(f"cannot read the price file {path} ({reason})") from None
    try:
        return ModelPrices.model_validate_json(raw)
    except ValidationError:
        raise PriceFileError(f"the price file {path} is not a valid price list") from None
