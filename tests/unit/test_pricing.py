"""Model prices (``config/model_prices.json``) and the per-call cost estimate (US-08 FR-010)."""

import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.pricing import ModelPrices, PriceFileError, load_model_prices
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app

PRICES = ModelPrices.model_validate(
    {
        "version": 1,
        "models": {
            "claude-a": {"input_per_mtok": "3.00", "output_per_mtok": "15.00"},
            "claude-a-long": {"input_per_mtok": "6.00", "output_per_mtok": "30.00"},
        },
    }
)


def _warnings(stream: io.StringIO) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [line for line in lines if line["event"] == "model_price_unknown"]


def test_should_price_input_and_output_tokens_when_the_model_is_listed() -> None:
    cost = PRICES.estimate_cost("claude-a", 1_000, 200)

    assert cost == Decimal("0.006000")  # 1000 * 3 / 1e6 + 200 * 15 / 1e6


def test_should_round_to_six_places_when_the_cost_is_tiny() -> None:
    assert PRICES.estimate_cost("claude-a", 1, 0) == Decimal("0.000003")
    assert PRICES.estimate_cost("claude-a", 0, 0) == Decimal("0.000000")


def test_should_use_the_longest_listed_id_when_the_model_id_is_dated() -> None:
    assert PRICES.estimate_cost("claude-a-20261001", 1_000_000, 0) == Decimal("3.000000")
    assert PRICES.estimate_cost("claude-a-long-20261001", 1_000_000, 0) == Decimal("6.000000")


def test_should_return_none_without_a_warning_when_no_model_answered() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)

    assert PRICES.estimate_cost(None, 100, 10) is None
    assert _warnings(stream) == []


def test_should_return_none_and_warn_once_when_the_model_is_unknown() -> None:
    prices = ModelPrices.model_validate(PRICES.model_dump())
    stream = io.StringIO()
    configure_logging("INFO", stream)

    first = prices.estimate_cost("claude-unknown", 100, 10)
    second = prices.estimate_cost("claude-unknown", 100, 10)
    other = prices.estimate_cost("claude-other", 100, 10)

    assert (first, second, other) == (None, None, None)
    assert [line["model_id"] for line in _warnings(stream)] == ["claude-unknown", "claude-other"]


def test_should_load_the_shipped_price_file_when_the_path_is_the_default() -> None:
    prices = load_model_prices(Settings().model_prices_path)

    assert prices.version >= 1
    assert prices.models
    assert all(price.input_per_mtok > 0 for price in prices.models.values())


def test_should_raise_when_the_price_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PriceFileError, match="cannot read"):
        load_model_prices(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"version": 1}',
        '{"version": 1, "models": {"m": {"input_per_mtok": "-1", "output_per_mtok": "1"}}}',
        '{"version": 0, "models": {}}',
    ],
)
def test_should_raise_without_echoing_the_content_when_the_price_file_is_invalid(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "prices.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(PriceFileError, match="not a valid price list") as raised:
        load_model_prices(path)

    assert content not in str(raised.value)


def test_should_fail_to_build_the_app_when_the_price_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PriceFileError):
        create_app(Settings(model_prices_path=str(tmp_path / "missing.json")), probes=[])
