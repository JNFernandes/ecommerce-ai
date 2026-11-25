"""LangGraph checkpointer on PostgreSQL over the existing async engine (research §5, FR-014).

The chat graph's state per conversation (thread id = conversation id) lives in
``chat_checkpoints`` and ``chat_checkpoint_writes`` (migration 0003). Each checkpoint row holds the
whole serialized checkpoint, channel values included, so no blob table is needed. Only the newest
checkpoint of a ``(thread_id, checkpoint_ns)`` is kept: storing one deletes the older ones and
their pending writes in the same transaction. Writes of a checkpoint that no longer exists are
dropped. ``task_path`` is not stored, so pending writes come back ordered by ``(task_id, idx)``.
Only the async API is implemented; the graph never calls the sync one.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from sqlalchemy import ColumnElement, RowMapping, and_, delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ecommerce_ai.infrastructure.db.tables import chat_checkpoint_writes, chat_checkpoints

_checkpoints = chat_checkpoints.c
_writes = chat_checkpoint_writes.c


def _thread(config: RunnableConfig) -> tuple[str, str]:
    configurable = config["configurable"]
    return str(configurable["thread_id"]), str(configurable.get("checkpoint_ns", ""))


def _config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }


class PostgresCheckpointSaver(BaseCheckpointSaver[str]):
    """``BaseCheckpointSaver`` on PostgreSQL; one transaction per call."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Read and write on ``engine``; values use LangGraph's default serializer."""
        super().__init__()
        self._engine = engine

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """The checkpoint named by ``config``, else the thread's newest one, or ``None``."""
        thread_id, checkpoint_ns = _thread(config)
        query = select(chat_checkpoints).where(
            _checkpoints.thread_id == thread_id, _checkpoints.checkpoint_ns == checkpoint_ns
        )
        if checkpoint_id := get_checkpoint_id(config):
            query = query.where(_checkpoints.checkpoint_id == checkpoint_id)
        query = query.order_by(_checkpoints.checkpoint_id.desc()).limit(1)
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
            if row is None:
                return None
            return await self._tuple(connection, row._mapping)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Checkpoints matching ``config``, ``filter`` (metadata subset) and ``before``, newest
        first, at most ``limit``."""
        conditions: list[ColumnElement[bool]] = []
        if config is not None:
            configurable = config["configurable"]
            conditions.append(_checkpoints.thread_id == str(configurable["thread_id"]))
            if "checkpoint_ns" in configurable:
                conditions.append(_checkpoints.checkpoint_ns == str(configurable["checkpoint_ns"]))
            if checkpoint_id := get_checkpoint_id(config):
                conditions.append(_checkpoints.checkpoint_id == checkpoint_id)
        if filter:
            conditions.append(_checkpoints.metadata.contains(filter))
        if before is not None and (before_id := get_checkpoint_id(before)):
            conditions.append(_checkpoints.checkpoint_id < before_id)
        query = (
            select(chat_checkpoints)
            .where(*conditions)
            .order_by(_checkpoints.checkpoint_id.desc())
            .limit(limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()
            tuples = [await self._tuple(connection, row._mapping) for row in rows]
        for item in tuples:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store ``checkpoint`` as the thread's only one; returns its config."""
        thread_id, checkpoint_ns = _thread(config)
        checkpoint_id = checkpoint["id"]
        kind, payload = self.serde.dumps_typed(checkpoint)
        same_thread = and_(
            _checkpoints.thread_id == thread_id,
            _checkpoints.checkpoint_ns == checkpoint_ns,
            _checkpoints.checkpoint_id != checkpoint_id,
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(chat_checkpoint_writes).where(
                    _writes.thread_id == thread_id,
                    _writes.checkpoint_ns == checkpoint_ns,
                    _writes.checkpoint_id != checkpoint_id,
                )
            )
            await connection.execute(delete(chat_checkpoints).where(same_thread))
            statement = insert(chat_checkpoints).values(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=config["configurable"].get("checkpoint_id"),
                type=kind,
                checkpoint=payload,
                metadata=get_checkpoint_metadata(config, metadata),
                created_at=datetime.fromisoformat(checkpoint["ts"]).astimezone(UTC),
            )
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                    set_={
                        "type": statement.excluded.type,
                        "checkpoint": statement.excluded.checkpoint,
                        "metadata": statement.excluded.metadata,
                    },
                )
            )
        return _config(thread_id, checkpoint_ns, checkpoint_id)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store the pending writes of ``task_id`` for the checkpoint named by ``config``.

        Regular writes already stored are kept; special ones (errors, interrupts) are replaced.
        """
        thread_id, checkpoint_ns = _thread(config)
        checkpoint_id = str(config["configurable"]["checkpoint_id"])
        async with self._engine.begin() as connection:
            stored = await connection.execute(
                select(
                    exists().where(
                        _checkpoints.thread_id == thread_id,
                        _checkpoints.checkpoint_ns == checkpoint_ns,
                        _checkpoints.checkpoint_id == checkpoint_id,
                    )
                )
            )
            if not stored.scalar_one():
                return  # superseded by a newer checkpoint
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                kind, payload = self.serde.dumps_typed(value)
                statement = insert(chat_checkpoint_writes).values(
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint_id,
                    task_id=task_id,
                    idx=idx,
                    channel=channel,
                    type=kind,
                    value=payload,
                )
                keys = ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"]
                if idx >= 0:
                    await connection.execute(statement.on_conflict_do_nothing(index_elements=keys))
                else:
                    await connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=keys,
                            set_={
                                "channel": statement.excluded.channel,
                                "type": statement.excluded.type,
                                "value": statement.excluded.value,
                            },
                        )
                    )

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint and pending write of the thread (FR-004)."""
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(chat_checkpoint_writes).where(_writes.thread_id == thread_id)
            )
            await connection.execute(
                delete(chat_checkpoints).where(_checkpoints.thread_id == thread_id)
            )

    def get_next_version(self, current: str | None, channel: None) -> str:
        """The next channel version: a zero-padded counter, so string order is numeric order."""
        number = 0 if current is None else int(current.split(".")[0])
        return f"{number + 1:032}"

    async def _tuple(self, connection: AsyncConnection, row: RowMapping) -> CheckpointTuple:
        thread_id, checkpoint_ns = row["thread_id"], row["checkpoint_ns"]
        checkpoint_id = row["checkpoint_id"]
        writes = await connection.execute(
            select(_writes.task_id, _writes.channel, _writes.type, _writes.value)
            .where(
                _writes.thread_id == thread_id,
                _writes.checkpoint_ns == checkpoint_ns,
                _writes.checkpoint_id == checkpoint_id,
            )
            .order_by(_writes.task_id, _writes.idx)
        )
        parent_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config=_config(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint=cast("Checkpoint", self.serde.loads_typed((row["type"], row["checkpoint"]))),
            metadata=cast("CheckpointMetadata", row["metadata"]),
            parent_config=_config(thread_id, checkpoint_ns, parent_id) if parent_id else None,
            pending_writes=[
                (write.task_id, write.channel, self.serde.loads_typed((write.type, write.value)))
                for write in writes
            ],
        )
