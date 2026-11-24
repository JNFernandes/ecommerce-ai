"""The triage consumer saves each event, then commits its offset and logs one line."""

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from ecommerce_ai.agents.triage_intake.model import TriageCause
from ecommerce_ai.agents.triage_intake.service import TriageIntakeService
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.consumer_state import ConsumerState
from ecommerce_ai.messaging.contracts import (
    ORDER_CANCELLED_TOPIC,
    PAYMENT_FAILED_TOPIC,
    STOCK_REJECTED_TOPIC,
)
from ecommerce_ai.messaging.kafka import ConsumedRecord, TopicPartition
from ecommerce_ai.messaging.triage_consumer import TriageConsumer
from tests.support.kafka import (
    FakeConsumer,
    make_record,
    order_cancelled_payload,
    payment_failed_payload,
    stock_rejected_payload,
)
from tests.support.polling import wait_until
from tests.support.stores import InMemoryDeadLetterStore, InMemoryTriageCaseStore, _UnitOfWork


class CheckingConsumer(FakeConsumer):
    """Records how many cases were saved at the moment of each commit."""

    def __init__(self, store: InMemoryTriageCaseStore, records: list[ConsumedRecord]) -> None:
        super().__init__(records)
        self.saved_at_commit: list[int] = []
        self._store = store

    async def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        self.saved_at_commit.append(len(self._store.evidence))
        await super().commit(offsets)


@pytest.fixture
def store() -> InMemoryTriageCaseStore:
    return InMemoryTriageCaseStore()


@pytest.fixture
def state() -> ConsumerState:
    return ConsumerState()


class Pacing:
    """Records reconnect waits and database-retry sleeps instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.sleeps: list[float] = []

    async def wait(self, _stop: asyncio.Event, delay: float) -> None:
        self.waits.append(delay)
        await asyncio.sleep(0)  # yield only, so a reconnect loop cannot starve the test

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)


class FractionRandom:
    """Returns ``fraction`` of every jitter range."""

    def __init__(self, fraction: float = 1.0) -> None:
        self.fraction = fraction

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.fraction


def _triage_consumer(
    consumers: FakeConsumer | list[FakeConsumer],
    store: InMemoryTriageCaseStore,
    state: ConsumerState,
    dead_letters: InMemoryDeadLetterStore | None = None,
    *,
    settings: Settings | None = None,
    pacing: Pacing | None = None,
    rng: FractionRandom | None = None,
) -> TriageConsumer:
    """A consumer on fakes; a list hands out one fake per connection, then repeats the last."""
    queue = consumers if isinstance(consumers, list) else [consumers]

    def factory() -> FakeConsumer:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    pacing = pacing or Pacing()
    return TriageConsumer(
        factory,
        TriageIntakeService(store),
        dead_letters or InMemoryDeadLetterStore(),
        state,
        settings or Settings(),
        poll_timeout_ms=5,
        wait=pacing.wait,
        sleep=pacing.sleep,
        rng=rng or FractionRandom(),
    )


async def _run_until(consumer: TriageConsumer, condition: Callable[[], bool]) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: condition() or task.done())
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def _three_orders() -> tuple[list[ConsumedRecord], dict[str, TriageCause]]:
    cancelled, paid, stocked = uuid4(), uuid4(), uuid4()
    records = [
        make_record(ORDER_CANCELLED_TOPIC, order_cancelled_payload(cancelled), offset=0),
        make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(paid), partition=1, offset=7),
        make_record(STOCK_REJECTED_TOPIC, stock_rejected_payload(stocked), offset=3),
    ]
    causes = {
        str(cancelled): TriageCause.CANCELLATION_REQUESTED,
        str(paid): TriageCause.PAYMENT_FAILED,
        str(stocked): TriageCause.INSUFFICIENT_STOCK,
    }
    return records, causes


async def test_should_record_one_case_per_order_when_each_topic_delivers_an_event(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    records, causes = _three_orders()
    fake = FakeConsumer(records)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 3)

    assert {str(case.order_id): case.cause for case in store.cases} == causes
    assert len(store.evidence) == 3
    assert fake.committed == {
        TopicPartition(ORDER_CANCELLED_TOPIC, 0): 1,
        TopicPartition(PAYMENT_FAILED_TOPIC, 1): 8,
        TopicPartition(STOCK_REJECTED_TOPIC, 0): 4,
    }
    assert fake.stopped
    assert (state.connected, state.caught_up, state.last_error) == (False, False, None)


async def test_should_commit_each_offset_only_after_its_event_is_saved(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    records, _ = _three_orders()
    fake = CheckingConsumer(store, records)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 3)

    assert fake.saved_at_commit == [1, 2, 3]


async def test_should_report_connected_and_caught_up_while_running(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()))])
    stop = asyncio.Event()
    task = asyncio.create_task(_triage_consumer(fake, store, state).run(stop))

    await wait_until(lambda: state.caught_up)

    assert state.connected
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_should_log_one_line_per_message_with_its_coordinates_and_outcome(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    order_id, event_id = uuid4(), uuid4()
    payload = payment_failed_payload(order_id, event_id=event_id, reason="card declined")
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payload, partition=2, offset=41)])
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 1)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    (line,) = [line for line in lines if line["event"] == "triage_message"]
    assert line["topic"] == PAYMENT_FAILED_TOPIC
    assert (line["partition"], line["offset"]) == (2, 41)
    assert (line["event_id"], line["order_id"]) == (str(event_id), str(order_id))
    assert line["outcome"] == "processed"
    assert "card declined" not in stream.getvalue()


async def test_should_commit_and_log_a_duplicate_when_an_event_is_redelivered(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    payload = payment_failed_payload(uuid4())
    fake = FakeConsumer(
        [
            make_record(PAYMENT_FAILED_TOPIC, payload, offset=0),
            make_record(PAYMENT_FAILED_TOPIC, payload, offset=1),
        ]
    )
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 2)

    assert len(store.evidence) == 1
    assert fake.committed == {TopicPartition(PAYMENT_FAILED_TOPIC, 0): 2}
    outcomes = [
        json.loads(line)["outcome"]
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "triage_message"
    ]
    assert outcomes == ["processed", "duplicate"]


NOT_UTF8 = bytes([0xFF]) + b"not json"


async def test_should_dead_letter_a_malformed_message_and_still_process_the_next_one(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    dead_letters = InMemoryDeadLetterStore()
    order_id = uuid4()
    fake = FakeConsumer(
        [
            make_record(PAYMENT_FAILED_TOPIC, NOT_UTF8, key=b"k-1", partition=3, offset=9),
            make_record(
                PAYMENT_FAILED_TOPIC, payment_failed_payload(order_id), partition=3, offset=10
            ),
        ]
    )
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(
        _triage_consumer(fake, store, state, dead_letters), lambda: len(fake.commits) == 2
    )

    (row,) = dead_letters.rows
    assert (row.topic, row.partition, row.offset, row.key) == (PAYMENT_FAILED_TOPIC, 3, 9, b"k-1")
    assert row.raw == NOT_UTF8
    assert row.reason == "payload is not valid UTF-8"
    assert [case.order_id for case in store.cases] == [order_id]
    assert fake.commits == [
        {TopicPartition(PAYMENT_FAILED_TOPIC, 3): 10},
        {TopicPartition(PAYMENT_FAILED_TOPIC, 3): 11},
    ]
    (dead_line, processed_line) = [
        line
        for line in map(json.loads, stream.getvalue().splitlines())
        if line["event"] == "triage_message"
    ]
    assert dead_line["outcome"] == "dead_lettered"
    assert "event_id" not in dead_line
    assert "order_id" not in dead_line
    assert processed_line["outcome"] == "processed"


async def test_should_not_retry_validation_when_a_message_is_dead_lettered(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    dead_letters = InMemoryDeadLetterStore()
    payload = {**payment_failed_payload(uuid4()), "version": 2}
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, payload)])

    await _run_until(
        _triage_consumer(fake, store, state, dead_letters), lambda: len(fake.commits) == 1
    )

    assert len(dead_letters.rows) == 1
    assert "version: literal_error" in dead_letters.rows[0].reason
    assert store.commits == 0


async def test_should_keep_an_empty_value_as_empty_bytes_when_dead_lettering(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    dead_letters = InMemoryDeadLetterStore()
    fake = FakeConsumer([make_record(STOCK_REJECTED_TOPIC, None)])

    await _run_until(
        _triage_consumer(fake, store, state, dead_letters), lambda: len(fake.commits) == 1
    )

    assert (dead_letters.rows[0].raw, dead_letters.rows[0].key) == (b"", None)


def _record(offset: int = 0) -> ConsumedRecord:
    return make_record(PAYMENT_FAILED_TOPIC, payment_failed_payload(uuid4()), offset=offset)


def _redelivering(record: ConsumedRecord) -> list[FakeConsumer]:
    """Two connections that both deliver ``record``, as Kafka does after a missing commit."""
    return [FakeConsumer([record]), FakeConsumer([record])]


async def test_should_reconnect_with_growing_bounded_delays_when_kafka_is_unreachable(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    settings = Settings(
        kafka_reconnect_backoff_base_seconds=1, kafka_reconnect_backoff_max_seconds=4
    )
    fake = FakeConsumer(start_error=ConnectionRefusedError("broker down"))
    consumer = _triage_consumer(fake, store, state, settings=settings, pacing=pacing)

    await _run_until(consumer, lambda: len(pacing.waits) >= 5)

    assert pacing.waits[:5] == [1, 2, 4, 4, 4]
    assert fake.stopped
    assert (state.connected, state.last_error) == (False, "ConnectionRefusedError")


async def test_should_jitter_each_reconnect_delay_within_the_upper_half_of_its_cap(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    settings = Settings(
        kafka_reconnect_backoff_base_seconds=2, kafka_reconnect_backoff_max_seconds=30
    )
    fake = FakeConsumer(start_error=ConnectionRefusedError("broker down"))
    consumer = _triage_consumer(
        fake, store, state, settings=settings, pacing=pacing, rng=FractionRandom(0.0)
    )

    await _run_until(consumer, lambda: len(pacing.waits) >= 2)

    assert pacing.waits[:2] == [1, 2]


async def test_should_clear_the_error_when_the_consumer_reconnects(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    down = FakeConsumer(start_error=ConnectionRefusedError("broker down"))
    up = FakeConsumer([_record()])

    await _run_until(
        _triage_consumer([down, up], store, state),
        lambda: len(up.commits) == 1 and state.connected,
    )

    assert state.last_error is None


async def test_should_stop_at_once_when_asked_during_a_reconnect_backoff(
    state: ConsumerState,
) -> None:
    settings = Settings(
        kafka_reconnect_backoff_base_seconds=30, kafka_reconnect_backoff_max_seconds=30
    )
    consumer = TriageConsumer(
        lambda: FakeConsumer(start_error=ConnectionRefusedError("broker down")),
        TriageIntakeService(InMemoryTriageCaseStore()),
        InMemoryDeadLetterStore(),
        state,
        settings,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await wait_until(lambda: state.last_error is not None)

    stop.set()

    await asyncio.wait_for(task, timeout=1)


async def test_should_retry_a_transient_database_error_without_committing_then_save(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    settings = Settings(
        db_retry_max_attempts=5, db_retry_backoff_base_seconds=0.5, db_retry_backoff_max_seconds=1
    )
    store.failures.extend([ConnectionResetError("gone"), ConnectionResetError("gone")])
    fake = CheckingConsumer(store, [_record()])

    await _run_until(
        _triage_consumer(fake, store, state, settings=settings, pacing=pacing),
        lambda: len(fake.commits) == 1,
    )

    assert pacing.sleeps == [0.5, 1]
    assert fake.saved_at_commit == [1]
    assert pacing.waits == []


async def test_should_reconnect_and_process_the_redelivery_when_database_retries_run_out(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    settings = Settings(db_retry_max_attempts=2)
    store.failures.extend([ConnectionResetError("gone"), ConnectionResetError("gone")])
    first, second = _redelivering(_record(offset=4))

    await _run_until(
        _triage_consumer([first, second], store, state, settings=settings, pacing=pacing),
        lambda: len(second.commits) == 1,
    )

    assert (first.commits, first.stopped) == ([], True)
    assert second.committed == {TopicPartition(PAYMENT_FAILED_TOPIC, 0): 5}
    assert len(store.cases) == 1
    assert (len(pacing.sleeps), len(pacing.waits)) == (1, 1)


async def test_should_not_retry_an_error_that_is_not_transient(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    store.failures.append(ValueError("a bug, not an outage"))
    first, second = _redelivering(_record())

    await _run_until(
        _triage_consumer([first, second], store, state, pacing=pacing),
        lambda: len(second.commits) == 1,
    )

    assert pacing.sleeps == []
    assert first.commits == []
    assert len(pacing.waits) == 1


async def test_should_retry_a_failed_dead_letter_write_like_any_database_error(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    dead_letters = InMemoryDeadLetterStore()
    dead_letters.failures.append(TimeoutError("database timeout"))
    fake = FakeConsumer([make_record(PAYMENT_FAILED_TOPIC, b"not json")])

    await _run_until(
        _triage_consumer(fake, store, state, dead_letters, pacing=pacing),
        lambda: len(fake.commits) == 1,
    )

    assert len(dead_letters.rows) == 1
    assert len(pacing.sleeps) == 1


class UnknownHighwaterConsumer(FakeConsumer):
    """A consumer that has not fetched partition metadata yet."""

    def highwater(self, partition: TopicPartition) -> int | None:
        return None


async def test_should_not_report_caught_up_while_the_highwater_is_unknown(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    fake = UnknownHighwaterConsumer([_record()])
    stop = asyncio.Event()
    task = asyncio.create_task(_triage_consumer(fake, store, state).run(stop))

    await wait_until(lambda: len(fake.commits) == 1)

    assert state.connected
    assert not state.caught_up
    stop.set()
    await asyncio.wait_for(task, timeout=2)


class LagRecorder(ConsumerState):
    """Keeps every lag the consumer reports."""

    def __init__(self) -> None:
        super().__init__()
        self.lags: list[int] = []

    def update_lag(self, lag: int) -> None:
        self.lags.append(lag)
        super().update_lag(lag)


async def test_should_report_lag_until_every_record_is_read() -> None:
    state = LagRecorder()
    fake = FakeConsumer([_record(offset=0), _record(offset=1)])

    await _run_until(
        _triage_consumer(fake, InMemoryTriageCaseStore(), state), lambda: len(fake.commits) == 2
    )

    assert state.lags[:2] == [1, 0]


class BlockingStore(InMemoryTriageCaseStore):
    """Blocks the second transaction forever, like a save cut short by shutdown."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.blocked = asyncio.Event()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_UnitOfWork]:
        self.calls += 1
        if self.calls == 2:
            self.blocked.set()
            await asyncio.Event().wait()
        async with super().transaction() as work:
            yield work


async def test_should_commit_saved_events_but_not_the_one_in_flight_when_cancelled(
    state: ConsumerState,
) -> None:
    store = BlockingStore()
    fake = FakeConsumer([_record(offset=0), _record(offset=1)])
    task = asyncio.create_task(_triage_consumer(fake, store, state).run(asyncio.Event()))
    await wait_until(store.blocked.is_set)

    task.cancel()
    await asyncio.wait({task})

    assert fake.commits == [{TopicPartition(PAYMENT_FAILED_TOPIC, 0): 1}]
    assert fake.stopped


async def test_should_log_and_go_on_when_closing_the_consumer_fails(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    pacing = Pacing()
    broken = FakeConsumer(
        start_error=ConnectionRefusedError("down"), stop_error=RuntimeError("close failed")
    )
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(
        _triage_consumer(broken, store, state, pacing=pacing), lambda: len(pacing.waits) >= 1
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    closing = {line["error"] for line in lines if line["event"] == "triage_consumer_close_failed"}
    assert closing == {"RuntimeError"}
    assert state.last_error == "ConnectionRefusedError"


async def test_should_log_no_payload_customer_id_or_reason_for_any_outcome(
    store: InMemoryTriageCaseStore, state: ConsumerState
) -> None:
    customer_reason = "payment failed: card 4111-1111-1111-1111 declined for Ana Silva"
    cancelled = order_cancelled_payload(uuid4(), reason=customer_reason)
    failed = payment_failed_payload(uuid4(), reason="insufficient funds on account PT50-0002")
    malformed = {**failed, "version": 2, "note": "do not log me"}
    records = [
        make_record(ORDER_CANCELLED_TOPIC, cancelled, offset=0),
        make_record(PAYMENT_FAILED_TOPIC, failed, offset=0),
        make_record(PAYMENT_FAILED_TOPIC, failed, offset=1),
        make_record(PAYMENT_FAILED_TOPIC, malformed, offset=2),
    ]
    fake = FakeConsumer(records)
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _run_until(_triage_consumer(fake, store, state), lambda: len(fake.commits) == 4)

    output = stream.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    outcomes = sorted(line["outcome"] for line in lines if line["event"] == "triage_message")
    assert outcomes == ["dead_lettered", "duplicate", "processed", "processed"]
    for secret in (
        str(cancelled["customerId"]),
        "customerId",
        "4111",
        "Ana Silva",
        "PT50-0002",
        "insufficient funds",
        "do not log me",
        "occurredAt",
    ):
        assert secret not in output
