"""Kafka consumer and producer ports and their aiokafka adapters (research 5, 7).

The triage consumer depends only on ``ConsumerPort`` and the plain ``TopicPartition`` and
``ConsumedRecord`` types below, and the outbox relay only on ``ProducerPort``, so unit tests drive
both with fakes. This is the only module that imports aiokafka, which ships no type information:
its values are converted to typed ones here.
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


class ProducerPort(Protocol):
    """The producer operations the outbox relay uses."""

    async def start(self) -> None:
        """Connect to the cluster."""
        ...

    async def stop(self) -> None:
        """Flush pending records and close the connections."""
        ...

    async def send(self, topic: str, key: bytes, value: bytes) -> None:
        """Send one record and return once every in-sync replica has acknowledged it."""
        ...


type ProducerFactory = Callable[[], ProducerPort]
"""Builds a fresh, not yet started producer (one per connection attempt)."""


class AIOKafkaProducerAdapter:
    """``ProducerPort`` over ``aiokafka.AIOKafkaProducer`` with ``acks=all`` and idempotence."""

    def __init__(self, settings: Settings) -> None:
        """Configure the producer; nothing connects until ``start``."""
        # Untyped library object; nothing is read back from it.
        self._producer = aiokafka.AIOKafkaProducer(
            bootstrap_servers=",".join(settings.kafka_brokers),
            acks="all",
            enable_idempotence=True,
        )

    async def start(self) -> None:
        """Connect to the cluster."""
        await self._producer.start()

    async def stop(self) -> None:
        """Flush pending records and close the connections."""
        await self._producer.stop()

    async def send(self, topic: str, key: bytes, value: bytes) -> None:
        """Send one record and wait for the acknowledgement of every in-sync replica."""
        await self._producer.send_and_wait(topic, value=value, key=key)
