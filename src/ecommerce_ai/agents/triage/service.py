"""Triage of one case per call: lock a settled case, run the graph, save its result (FR-001).

The case stays locked for the whole run; its result, outbox event and COMPLETED status are saved
in the same transaction, or nothing is (FR-015). After the commit one ``triage_case`` log line
records ids, source, revisions, tokens and latency, never evidence, reasons or text (FR-020).
The run is one ``triage.run`` span (ids, source, revisions), the parent of its node and model spans.
"""

from datetime import timedelta

import structlog

from ecommerce_ai.agents.triage.graph import TriageGraph
from ecommerce_ai.agents.triage.ports import Clock, TriageCaseQueue
from ecommerce_ai.telemetry.attributes import CASE_ID, ORDER_ID, TRIAGE_REVISIONS, TRIAGE_SOURCE
from ecommerce_ai.telemetry.spans import TRIAGE_RUN
from ecommerce_ai.telemetry.tracing import span

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
            ids = {CASE_ID: str(case.case_id), ORDER_ID: str(case.order_id)}
            with span(TRIAGE_RUN, ids) as run:
                # Every log line of the run (model calls included) carries the order id (VII).
                with structlog.contextvars.bound_contextvars(order_id=str(case.order_id)):
                    result = await self._graph.run(case)
                run.set_attribute(TRIAGE_SOURCE, result.source.value)
                run.set_attribute(TRIAGE_REVISIONS, result.revisions)
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
