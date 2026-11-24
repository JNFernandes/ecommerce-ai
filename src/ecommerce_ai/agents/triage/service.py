"""Triage of one case per call: lock a settled case, run the graph, save its result (FR-001).

The case stays locked for the whole run; its result, outbox event and COMPLETED status are saved
in the same transaction, or nothing is (FR-015). After the commit one ``triage_case`` log line
records ids, source, revisions, tokens and latency, never evidence, reasons or text (FR-020).
"""

from datetime import timedelta

import structlog

from ecommerce_ai.agents.triage.graph import TriageGraph
from ecommerce_ai.agents.triage.ports import Clock, TriageCaseQueue

logger = structlog.get_logger(__name__)


class TriageService:
    """Explains settled triage cases, one per ``triage_one`` call."""

    def __init__(
        self,
        queue: TriageCaseQueue,
        graph: TriageGraph,
        clock: Clock,
        *,
        settle_seconds: float,
    ) -> None:
        """Take cases from ``queue`` once ``settle_seconds`` passed since their last event."""
        self._queue = queue
        self._graph = graph
        self._clock = clock
        self._settle = timedelta(seconds=settle_seconds)

    async def triage_one(self) -> bool:
        """Triage the oldest settled case; ``False`` when no case is settled."""
        async with self._queue.next_settled(self._clock.now() - self._settle) as claim:
            case = claim.case
            if case is None:
                return False
            # Every log line of the run (model calls included) carries the order id (VII).
            with structlog.contextvars.bound_contextvars(order_id=str(case.order_id)):
                result = await self._graph.run(case)
            await claim.complete(result)
        logger.info(
            "triage_case",
            case_id=str(case.case_id),
            order_id=str(case.order_id),
            cause=result.cause.value,
            source=result.source.value,
            suggested_action=result.suggested_action.value,
            revisions=result.revisions,
            violations=[violation.rule.value for violation in result.violations],
            # Not ``*_tokens``: the log redactor hides every field whose name contains "token".
            usage={"input": result.input_tokens, "output": result.output_tokens},
            latency_ms=result.latency_ms,
            prompt_version=result.prompt_version,
            model_id=result.model_id,
            outcome="completed",
        )
        return True
