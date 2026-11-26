"""Per-call usage recording shared by both agents (US-08 FR-010, FR-011).

``LlmUsage`` prices a call (``CostEstimator``) and stores its ``LlmCall`` (``LlmCallRecorder``).
Recording fails open: a failure is logged by class name only and never changes the run's result.
``LlmUsage()`` with neither port records nothing and prices nothing.
"""

from decimal import Decimal

import structlog

from ecommerce_ai.agents.ports import CostEstimator, LlmCall, LlmCallRecorder

_logger = structlog.get_logger(__name__)


class LlmUsage:
    """Prices and records the model calls of a graph."""

    def __init__(
        self, recorder: LlmCallRecorder | None = None, prices: CostEstimator | None = None
    ) -> None:
        """Store records in ``recorder`` (``None``: off), priced by ``prices`` (``None``: none)."""
        self._recorder = recorder
        self._prices = prices

    def cost(self, model_id: str | None, input_tokens: int, output_tokens: int) -> Decimal | None:
        """Estimated USD cost of a call, ``None`` when it cannot be priced."""
        if self._prices is None:
            return None
        return self._prices.estimate_cost(model_id, input_tokens, output_tokens)

    async def record(self, call: LlmCall) -> None:
        """Store ``call``; a failure is logged (class name only) and does not propagate."""
        if self._recorder is None:
            return
        try:
            await self._recorder.record(call)
        except Exception as error:
            _logger.warning(
                "llm_call_record_failed",
                error_type=type(error).__name__,
                kind=call.kind.value,
                outcome=call.outcome.value,
            )
