"""The PostgreSQL guard audit stores content-free events and answers the cooldown queries."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardrailEventStore,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.infrastructure.db.guardrail_event_repository import GuardrailEventRepository
from ecommerce_ai.infrastructure.db.tables import guardrail_events

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class StepClock:
    """A UTC clock moved by hand."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _event(
    stage: GuardStage = GuardStage.INPUT,
    action: GuardAction = GuardAction.BLOCKED,
    severity: GuardSeverity = GuardSeverity.HIGH,
    rule: str = "instruction_override",
) -> GuardEvent:
    return GuardEvent(stage=stage, rule=rule, action=action, severity=severity)


async def _rows(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        result = await connection.execute(select(guardrail_events))
        return [dict(row) for row in result.mappings()]


async def test_should_store_one_row_per_event_when_appended(db_engine: AsyncEngine) -> None:
    store: GuardrailEventStore = GuardrailEventRepository(db_engine, StepClock())
    conversation_id = uuid4()

    await store.append(
        "user-1",
        conversation_id,
        "req-1",
        [_event(), _event(GuardStage.OUTPUT, GuardAction.REPLACED, rule="canary")],
    )

    rows = sorted(await _rows(db_engine), key=lambda row: str(row["stage"]))
    assert [{k: v for k, v in row.items() if k != "id"} for row in rows] == [
        {
            "user_id": "user-1",
            "conversation_id": conversation_id,
            "request_id": "req-1",
            "stage": "input",
            "rule": "instruction_override",
            "action": "blocked",
            "severity": "high",
            "created_at": T0,
        },
        {
            "user_id": "user-1",
            "conversation_id": conversation_id,
            "request_id": "req-1",
            "stage": "output",
            "rule": "canary",
            "action": "replaced",
            "severity": "high",
            "created_at": T0,
        },
    ]


async def test_should_store_nothing_when_there_are_no_events(db_engine: AsyncEngine) -> None:
    await GuardrailEventRepository(db_engine).append("user-1", None, None, [])

    assert await _rows(db_engine) == []


async def test_should_stamp_events_with_the_utc_time_when_no_clock_is_given(
    db_engine: AsyncEngine,
) -> None:
    before = datetime.now(UTC)
    await GuardrailEventRepository(db_engine).append("user-1", None, None, [_event()])

    (row,) = await _rows(db_engine)
    created_at = row["created_at"]
    assert isinstance(created_at, datetime)
    assert created_at.utcoffset() == timedelta(0)
    assert created_at >= before - timedelta(seconds=1)


async def test_should_count_only_recent_input_blocks_of_the_user_at_or_above_the_severity(
    db_engine: AsyncEngine,
) -> None:
    clock = StepClock()
    store = GuardrailEventRepository(db_engine, clock)
    await store.append("user-1", None, None, [_event()])  # T0, too old below
    clock.now = T0 + timedelta(minutes=10)
    await store.append(
        "user-1",
        None,
        None,
        [
            _event(severity=GuardSeverity.MEDIUM),
            _event(severity=GuardSeverity.HIGH),
            _event(severity=GuardSeverity.LOW),  # below the severity
            _event(GuardStage.TOOL, GuardAction.SANITIZED),  # not an input block
            _event(GuardStage.OUTPUT, GuardAction.REPLACED),  # not an input block
        ],
    )
    await store.append("user-2", None, None, [_event()])  # another user

    since = T0 + timedelta(minutes=5)
    assert await store.count_blocks_since("user-1", since, GuardSeverity.MEDIUM) == 2
    assert await store.count_blocks_since("user-1", since, GuardSeverity.LOW) == 3
    assert await store.count_blocks_since("user-1", since, GuardSeverity.HIGH) == 1
    assert await store.count_blocks_since("user-1", T0, GuardSeverity.MEDIUM) == 3
    assert await store.count_blocks_since("nobody", T0, GuardSeverity.LOW) == 0


async def test_should_give_the_newest_cooldown_of_the_user(db_engine: AsyncEngine) -> None:
    clock = StepClock()
    store = GuardrailEventRepository(db_engine, clock)
    cooldown = _event(action=GuardAction.COOLDOWN, rule="cooldown")
    assert await store.latest_cooldown_at("user-1") is None

    await store.append("user-1", None, None, [cooldown])
    clock.now = T0 + timedelta(minutes=30)
    await store.append("user-1", None, None, [cooldown, _event()])
    clock.now = T0 + timedelta(hours=1)
    await store.append("user-2", None, None, [cooldown])
    await store.append("user-1", None, None, [_event()])  # newer, but not a cooldown

    assert await store.latest_cooldown_at("user-1") == T0 + timedelta(minutes=30)
