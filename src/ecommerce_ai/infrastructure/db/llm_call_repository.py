"""PostgreSQL usage records (``llm_calls``, US-08 FR-010): insert only.

Each ``LlmCall`` becomes one row stamped with the current UTC time; its cost was priced by the
agent (``LlmUsage`` over ``ModelPrices``), so the row and the ``llm.call`` span agree.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.ports import LlmCall
from ecommerce_ai.infrastructure.db.tables import llm_calls


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LlmCallRepository:
    """``LlmCallRecorder`` over the async engine; each record is one short transaction."""

    def __init__(self, engine: AsyncEngine, now: Callable[[], datetime] = _utc_now) -> None:
        """Store records on ``engine``, stamped by ``now`` (UTC, timezone-aware)."""
        self._engine = engine
        self._now = now

    async def record(self, call: LlmCall) -> None:
        """Insert ``call`` as a new row."""
        row = {
            "id": uuid4(),
            "kind": call.kind.value,
            "conversation_id": call.conversation_id,
            "case_id": call.case_id,
            "request_id": call.request_id,
            "model_id": call.model_id,
            "prompt_version": call.prompt_version,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "estimated_cost_usd": call.estimated_cost_usd,
            "latency_ms": call.latency_ms,
            "outcome": call.outcome.value,
            "created_at": self._now(),
        }
        async with self._engine.begin() as connection:
            await connection.execute(llm_calls.insert(), row)
