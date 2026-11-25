"""``PostgresCheckpointSaver`` on PostgreSQL: the saver API, newest-only retention, a graph run
resumed by a second saver instance, and conversation delete removing the rows (FR-004, FR-014)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, empty_checkpoint
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.assistant.model import AssistantText, UserTurn
from ecommerce_ai.infrastructure.db.checkpoint_saver import PostgresCheckpointSaver
from ecommerce_ai.infrastructure.db.conversation_repository import ConversationRepository
from ecommerce_ai.infrastructure.db.tables import chat_checkpoint_writes, chat_checkpoints
from tests.support.chat import chat_graph, collect_run
from tests.support.llm import ScriptedChatModel, text_turn

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
METADATA: CheckpointMetadata = {"source": "loop", "step": 1}


def _thread(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint(checkpoint_id: str, transcript: list[str]) -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"transcript": transcript}
    checkpoint["channel_versions"] = {"transcript": "1"}
    return checkpoint


async def _count(engine: AsyncEngine, table: Table, thread_id: str) -> int:
    async with engine.connect() as connection:
        count: int = (
            await connection.execute(
                select(func.count()).select_from(table).where(table.c.thread_id == thread_id)
            )
        ).scalar_one()
    return count


async def test_should_return_the_stored_checkpoint_with_metadata_and_writes(
    db_engine: AsyncEngine,
) -> None:
    saver = PostgresCheckpointSaver(db_engine)
    stored = await saver.aput(
        _thread("t-1"), _checkpoint("0001", ["hello"]), METADATA, {"transcript": "1"}
    )
    await saver.aput_writes(stored, [("transcript", ["late"]), ("other", 7)], task_id="task-1")

    loaded = await saver.aget_tuple(_thread("t-1"))

    assert loaded is not None
    assert loaded.config == stored
    assert loaded.checkpoint["channel_values"] == {"transcript": ["hello"]}
    assert loaded.metadata == METADATA
    assert loaded.parent_config is None
    assert loaded.pending_writes == [("task-1", "transcript", ["late"]), ("task-1", "other", 7)]
    assert await saver.aget_tuple(stored) == loaded
    assert await saver.aget_tuple(_thread("unknown")) is None


async def test_should_keep_only_the_newest_checkpoint_of_a_thread(db_engine: AsyncEngine) -> None:
    saver = PostgresCheckpointSaver(db_engine)
    first = await saver.aput(
        _thread("t-1"), _checkpoint("0001", ["one"]), METADATA, {"transcript": "1"}
    )
    await saver.aput_writes(first, [("transcript", ["pending"])], task_id="task-1")

    second = await saver.aput(first, _checkpoint("0002", ["two"]), METADATA, {"transcript": "2"})
    await saver.aput_writes(first, [("transcript", ["stale"])], task_id="task-2")

    loaded = await saver.aget_tuple(_thread("t-1"))
    assert loaded is not None
    assert loaded.config == second
    assert loaded.parent_config == first
    assert loaded.pending_writes == []
    assert await saver.aget_tuple(first) is None
    assert await _count(db_engine, chat_checkpoints, "t-1") == 1
    assert await _count(db_engine, chat_checkpoint_writes, "t-1") == 0


async def test_should_keep_regular_writes_and_replace_special_ones(db_engine: AsyncEngine) -> None:
    saver = PostgresCheckpointSaver(db_engine)
    stored = await saver.aput(_thread("t-1"), _checkpoint("0001", []), METADATA, {})
    await saver.aput_writes(stored, [("transcript", "first"), ("__error__", "e1")], task_id="t")
    await saver.aput_writes(stored, [("transcript", "second"), ("__error__", "e2")], task_id="t")

    loaded = await saver.aget_tuple(stored)

    assert loaded is not None
    assert loaded.pending_writes is not None
    assert sorted(loaded.pending_writes) == [("t", "__error__", "e2"), ("t", "transcript", "first")]


async def test_should_list_checkpoints_by_thread_metadata_and_limit(
    db_engine: AsyncEngine,
) -> None:
    saver = PostgresCheckpointSaver(db_engine)
    await saver.aput(_thread("t-1"), _checkpoint("0001", ["a"]), METADATA, {})
    await saver.aput(_thread("t-2"), _checkpoint("0002", ["b"]), {"source": "input"}, {})

    by_thread = [item.config async for item in saver.alist(_thread("t-1"))]
    by_metadata = [item.config async for item in saver.alist(None, filter={"source": "input"})]
    everything = [item.config async for item in saver.alist(None)]
    limited = [item.config async for item in saver.alist(None, limit=1)]
    before = [
        item.config
        async for item in saver.alist(None, before={"configurable": {"checkpoint_id": "0002"}})
    ]

    assert [c["configurable"]["thread_id"] for c in by_thread] == ["t-1"]
    assert [c["configurable"]["thread_id"] for c in by_metadata] == ["t-2"]
    assert [c["configurable"]["checkpoint_id"] for c in everything] == ["0002", "0001"]
    assert [c["configurable"]["checkpoint_id"] for c in limited] == ["0002"]
    assert [c["configurable"]["checkpoint_id"] for c in before] == ["0001"]


async def test_should_delete_every_row_of_the_thread(db_engine: AsyncEngine) -> None:
    saver = PostgresCheckpointSaver(db_engine)
    stored = await saver.aput(_thread("t-1"), _checkpoint("0001", []), METADATA, {})
    await saver.aput_writes(stored, [("transcript", [])], task_id="task-1")
    kept = await saver.aput(_thread("t-2"), _checkpoint("0002", []), METADATA, {})

    await saver.adelete_thread("t-1")

    assert await saver.aget_tuple(_thread("t-1")) is None
    assert await _count(db_engine, chat_checkpoint_writes, "t-1") == 0
    assert await saver.aget_tuple(kept) is not None


async def test_should_resume_a_graph_run_with_a_new_saver_instance(db_engine: AsyncEngine) -> None:
    conversation_id = uuid4()
    first_model = ScriptedChatModel([text_turn("Let me check that for you.")])
    await collect_run(
        chat_graph(first_model, checkpointer=PostgresCheckpointSaver(db_engine)),
        conversation_id,
        "Where is my order?",
    )

    second_model = ScriptedChatModel([text_turn("The one you asked about.")])
    _, outcome = await collect_run(
        chat_graph(second_model, checkpointer=PostgresCheckpointSaver(db_engine)),
        conversation_id,
        "Which one?",
    )

    assert outcome.error is None
    (request,) = second_model.requests
    assert request.transcript == (
        UserTurn(text="Where is my order?"),
        AssistantText(text="Let me check that for you."),
        UserTurn(text="Which one?"),
    )
    assert await _count(db_engine, chat_checkpoints, str(conversation_id)) == 1


async def test_should_remove_checkpoint_rows_when_the_conversation_is_deleted(
    db_engine: AsyncEngine,
) -> None:
    repository = ConversationRepository(db_engine)
    conversation = await repository.create("user-1", None, None, NOW)
    conversation_id: UUID = conversation.id
    model = ScriptedChatModel([text_turn("Answer.")])
    await collect_run(
        chat_graph(model, checkpointer=PostgresCheckpointSaver(db_engine)),
        conversation_id,
        "Question?",
    )
    assert await _count(db_engine, chat_checkpoints, str(conversation_id)) == 1

    assert await repository.delete("user-1", conversation_id)

    assert await _count(db_engine, chat_checkpoints, str(conversation_id)) == 0
    assert await _count(db_engine, chat_checkpoint_writes, str(conversation_id)) == 0
