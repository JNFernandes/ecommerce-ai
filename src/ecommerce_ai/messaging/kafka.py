"""Kafka consumer port and its aiokafka adapter (research 5).

The triage consumer depends only on ``ConsumerPort`` and the plain ``TopicPartition`` and
``ConsumedRecord`` types below, so unit tests drive it with a fake. This is the only module that
imports aiokafka, which ships no type information: its values are converted to typed ones here.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple, Protocol

import aiokafka
from aiokafka.structs import TopicPartition as KafkaTopicPartition

from ecommerce_ai.infrastructure.settings import Settings


class TopicPartition(NamedTuple):
    """One partition of one topic."""

    topic: str
    partition: int


@dataclass(frozen=True, slots=True)
class ConsumedRecord:
    """One Kafka message, as raw bytes (decoding is the caller's job)."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None


class ConsumerPort(Protocol):
    """The consumer operations the triage consumer uses."""

    async def start(self) -> None:
        """Connect and join the consumer group."""
        ...

    async def stop(self) -> None:
        """Leave the group and close the connections, without committing."""
        ...

    async def getmany(self, timeout_ms: int) -> dict[TopicPartition, list[ConsumedRecord]]:
        """Return the next records per partition, waiting at most ``timeout_ms``."""
        ...

    async def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        """Commit ``offsets`` (the offset of the next record to read, per partition)."""
        ...

    def assignment(self) -> set[TopicPartition]:
        """Partitions currently assigned to this member."""
        ...

    def highwater(self, partition: TopicPartition) -> int | None:
        """Offset after the last record of ``partition``, if known yet."""
        ...

    async def position(self, partition: TopicPartition) -> int:
        """Offset of the next record this consumer will read from ``partition``."""
        ...


type ConsumerFactory = Callable[[], ConsumerPort]
"""Builds a fresh, not yet started consumer (one per connection attempt)."""


def _to_kafka(partition: TopicPartition) -> KafkaTopicPartition:
    return KafkaTopicPartition(partition.topic, partition.partition)


class AIOKafkaConsumerAdapter:
    """``ConsumerPort`` over ``aiokafka.AIOKafkaConsumer`` with manual commits."""

    def __init__(self, settings: Settings, topics: Iterable[str]) -> None:
        """Configure the consumer; nothing connects until ``start``."""
        # Untyped library object; every value read from it is converted below.
        self._consumer = aiokafka.AIOKafkaConsumer(
            *topics,
            bootstrap_servers=",".join(settings.kafka_brokers),
            group_id=settings.kafka_consumer_group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    async def start(self) -> None:
        """Connect and join the consumer group."""
        await self._consumer.start()

    async def stop(self) -> None:
        """Leave the group and close the connections, without committing."""
        await self._consumer.stop()

    async def getmany(self, timeout_ms: int) -> dict[TopicPartition, list[ConsumedRecord]]:
        """Return the next records per partition, waiting at most ``timeout_ms``."""
        batches = await self._consumer.getmany(timeout_ms=timeout_ms)
        return {
            TopicPartition(str(tp.topic), int(tp.partition)): [
                ConsumedRecord(
                    topic=str(record.topic),
                    partition=int(record.partition),
                    offset=int(record.offset),
                    key=record.key,
                    value=record.value,
                )
                for record in records
            ]
            for tp, records in batches.items()
        }

    async def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        """Commit ``offsets`` (the offset of the next record to read, per partition)."""
        await self._consumer.commit({_to_kafka(tp): offset for tp, offset in offsets.items()})

    def assignment(self) -> set[TopicPartition]:
        """Partitions currently assigned to this member."""
        return {
            TopicPartition(str(tp.topic), int(tp.partition)) for tp in self._consumer.assignment()
        }

    def highwater(self, partition: TopicPartition) -> int | None:
        """Offset after the last record of ``partition``, if known yet."""
        value = self._consumer.highwater(_to_kafka(partition))
        return None if value is None else int(value)

    async def position(self, partition: TopicPartition) -> int:
        """Offset of the next record this consumer will read from ``partition``."""
        return int(await self._consumer.position(_to_kafka(partition)))
