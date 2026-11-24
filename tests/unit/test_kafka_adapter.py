"""The aiokafka adapter: manual commits, group from settings and typed values."""

from collections.abc import Mapping
from types import SimpleNamespace

import aiokafka
import pytest
from aiokafka.structs import TopicPartition as KafkaTopicPartition

from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.messaging.kafka import AIOKafkaConsumerAdapter, ConsumedRecord, TopicPartition

TP = KafkaTopicPartition("payments.payment-failed", 1)


class StubAIOKafkaConsumer:
    """Records the configuration and calls the adapter makes."""

    def __init__(self, *topics: str, **config: object) -> None:
        self.topics = topics
        self.config = config
        self.calls: list[str] = []
        self.committed: Mapping[KafkaTopicPartition, int] = {}

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def getmany(self, *, timeout_ms: int) -> dict[KafkaTopicPartition, list[object]]:
        record = SimpleNamespace(topic=TP.topic, partition=1, offset=7, key=b"k", value=b"{}")
        return {TP: [record]}

    async def commit(self, offsets: Mapping[KafkaTopicPartition, int]) -> None:
        self.committed = offsets

    def assignment(self) -> set[KafkaTopicPartition]:
        return {TP}

    def highwater(self, partition: KafkaTopicPartition) -> int | None:
        assert isinstance(partition, KafkaTopicPartition)
        return 10 if partition == TP else None

    async def position(self, partition: KafkaTopicPartition) -> int:
        assert isinstance(partition, KafkaTopicPartition)
        return 8


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> AIOKafkaConsumerAdapter:
    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", StubAIOKafkaConsumer)
    settings = Settings(kafka_brokers=["k1:9092", "k2:9092"], kafka_consumer_group="ai-test")
    return AIOKafkaConsumerAdapter(settings, ["orders.order-cancelled", TP.topic])


def _stub(adapter: AIOKafkaConsumerAdapter) -> StubAIOKafkaConsumer:
    stub = adapter._consumer
    assert isinstance(stub, StubAIOKafkaConsumer)
    return stub


def test_should_disable_auto_commit_and_read_from_the_earliest_offset(
    adapter: AIOKafkaConsumerAdapter,
) -> None:
    stub = _stub(adapter)

    assert stub.topics == ("orders.order-cancelled", TP.topic)
    assert stub.config == {
        "bootstrap_servers": "k1:9092,k2:9092",
        "group_id": "ai-test",
        "enable_auto_commit": False,
        "auto_offset_reset": "earliest",
    }


async def test_should_convert_records_and_partitions_to_typed_values(
    adapter: AIOKafkaConsumerAdapter,
) -> None:
    partition = TopicPartition(TP.topic, 1)

    assert await adapter.getmany(timeout_ms=100) == {
        partition: [ConsumedRecord(TP.topic, 1, 7, b"k", b"{}")]
    }
    assert adapter.assignment() == {partition}
    assert adapter.highwater(partition) == 10
    assert adapter.highwater(TopicPartition(TP.topic, 2)) is None
    assert await adapter.position(partition) == 8


async def test_should_commit_with_aiokafka_partitions_and_delegate_start_and_stop(
    adapter: AIOKafkaConsumerAdapter,
) -> None:
    await adapter.start()
    await adapter.commit({TopicPartition(TP.topic, 1): 8})
    await adapter.stop()

    stub = _stub(adapter)
    assert stub.calls == ["start", "stop"]
    assert stub.committed == {TP: 8}
    assert all(isinstance(tp, KafkaTopicPartition) for tp in stub.committed)
