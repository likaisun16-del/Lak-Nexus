"""迁移准备层测试：验证 SQLite 快照、向量适配和图召回接口均可离线运行。"""

from __future__ import annotations

from pathlib import Path

import pytest

from likai_nexus.errors import MigrationError
from likai_nexus.memory.context_builder import ContextBuilder, MemoryCandidate
from likai_nexus.memory.postgres_vector import backfill_pending_memories
from likai_nexus.memory.retrieval_adapters import VectorMemoryRetriever
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.database import Database
from likai_nexus.storage.memory_repository import MemoryRepository
from likai_nexus.storage.preference_repository import PreferenceRepository
from likai_nexus.storage.session_repository import SessionRepository
from likai_nexus.storage.snapshot import (
    PORTABLE_TABLES,
    PortableSnapshot,
    SQLiteSnapshotExporter,
    restore_sqlite_snapshot,
)
from likai_nexus.storage.task_repository import TaskRepository


def _seed_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    tasks = TaskRepository(database)
    tasks.create("task-1", "验证 SQLite 快照", ReviewMode.STRICT)
    sessions = SessionRepository(database)
    session = sessions.create("迁移测试")
    user = sessions.add_message(
        session["session_id"], "user", "保留这段历史", parent_message_id=None, task_id="task-1"
    )
    sessions.add_message(
        session["session_id"],
        "assistant",
        "历史已保存",
        parent_message_id=user["message_id"],
        task_id="task-1",
    )
    PreferenceRepository(database).set("language", "zh-CN")
    MemoryRepository(database).create("project", "项目使用 SQLite", "task", "task-1")
    return database


def test_sqlite_snapshot_round_trip_preserves_current_data(tmp_path: Path) -> None:
    source = _seed_database(tmp_path / "source.sqlite3")
    snapshot = SQLiteSnapshotExporter(source).export()
    snapshot_path = tmp_path / "migration" / "snapshot.json"
    snapshot.write_json(snapshot_path)
    loaded = PortableSnapshot.read_json(snapshot_path)

    target = Database(tmp_path / "target.sqlite3")
    counts = restore_sqlite_snapshot(target, loaded)

    assert counts["tasks"] == 1
    assert all(table in loaded.tables for table in PORTABLE_TABLES)
    with target.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT active_leaf_id FROM sessions").fetchone()[0]
        assert connection.execute("SELECT content FROM memories").fetchone()[0] == "项目使用 SQLite"


def test_snapshot_restore_rejects_non_empty_target_and_unknown_table(tmp_path: Path) -> None:
    source = _seed_database(tmp_path / "source.sqlite3")
    snapshot = SQLiteSnapshotExporter(source).export()
    target = _seed_database(tmp_path / "target.sqlite3")

    with pytest.raises(MigrationError, match="目标数据库非空"):
        restore_sqlite_snapshot(target, snapshot)

    invalid = snapshot.to_dict()
    invalid["tables"] = {**invalid["tables"], "unknown": []}  # type: ignore[index]
    with pytest.raises(MigrationError, match="未知表"):
        PortableSnapshot.from_dict(invalid)


class FakeEmbedder:
    """离线向量模型替身，记录查询和写入文本。"""

    dimension = 2

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> tuple[float, float]:
        self.texts.append(text)
        return (1.0, 0.0)


class FakeVectorIndex:
    """离线向量数据库替身，只验证适配器契约参数。"""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, tuple[float, ...]]] = []
        self.deletions: list[str] = []
        self.searches: list[tuple[tuple[float, ...], float, int]] = []

    def upsert(self, memory_id: str, content_hash: str, embedding: tuple[float, ...]) -> None:
        self.upserts.append((memory_id, content_hash, embedding))

    def delete(self, memory_id: str) -> None:
        self.deletions.append(memory_id)

    def search(
        self, embedding: tuple[float, ...], *, threshold: float, limit: int
    ) -> tuple[MemoryCandidate, ...]:
        self.searches.append((embedding, threshold, limit))
        return (MemoryCandidate("memory-1", 0.88),)


def test_vector_retriever_is_provider_and_index_agnostic() -> None:
    embedder = FakeEmbedder()
    index = FakeVectorIndex()
    retriever = VectorMemoryRetriever(embedder, index)

    retriever.upsert("memory-1", "hash-1", "项目使用 SQLite")
    candidates = retriever.search("如何验证项目", threshold=0.7, limit=3)
    retriever.delete("memory-1")

    assert candidates == (MemoryCandidate("memory-1", 0.88),)
    assert embedder.texts == ["项目使用 SQLite", "如何验证项目"]
    assert index.upserts == [("memory-1", "hash-1", (1.0, 0.0))]
    assert index.searches == [((1.0, 0.0), 0.7, 3)]
    assert index.deletions == ["memory-1"]


def test_backfill_updates_memory_embedding_status(tmp_path: Path) -> None:
    database = Database(tmp_path / "backfill.sqlite3")
    database.initialize()
    memories = MemoryRepository(database)
    memory = memories.create("project", "项目使用 SQLite", "user")
    retriever = VectorMemoryRetriever(FakeEmbedder(), FakeVectorIndex())

    result = backfill_pending_memories(memories, retriever, limit=5)

    assert result == {"ready": 1, "failed": 0}
    assert memories.get(memory["memory_id"])["embedding_status"] == "ready"


class FailingVectorIndex(FakeVectorIndex):
    """模拟向量索引写入失败。"""

    def upsert(self, memory_id: str, content_hash: str, embedding: tuple[float, ...]) -> None:
        del memory_id, content_hash, embedding
        raise RuntimeError("index unavailable")


def test_backfill_marks_failed_index_write(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-backfill.sqlite3")
    database.initialize()
    memories = MemoryRepository(database)
    memory = memories.create("project", "项目使用 SQLite", "user")
    retriever = VectorMemoryRetriever(FakeEmbedder(), FailingVectorIndex())

    result = backfill_pending_memories(memories, retriever, limit=5)

    assert result == {"ready": 0, "failed": 1}
    assert memories.get(memory["memory_id"])["embedding_status"] == "failed"


class FixedRetriever:
    """离线主检索器替身。"""

    def search(
        self, query: str, *, threshold: float, limit: int
    ) -> tuple[MemoryCandidate, ...]:
        return (MemoryCandidate("seed", 0.9),)


class FakeGraphAdapter:
    """离线 Neo4j 图适配器替身，验证种子和预算传递。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def upsert_memory(
        self,
        memory_id: str,
        memory_type: str,
        source_type: str,
        source_ref: str | None,
        content_hash: str,
    ) -> None:
        del memory_id, memory_type, source_type, source_ref, content_hash

    def disable_memory(self, memory_id: str) -> None:
        del memory_id

    def expand(self, seed_memory_ids: tuple[str, ...], *, limit: int):
        self.calls.append((seed_memory_ids, limit))
        return (MemoryCandidate("related", 0.42),)


def test_context_builder_can_append_graph_candidates_with_total_budget(tmp_path: Path) -> None:
    database = Database(tmp_path / "graph.sqlite3")
    database.initialize()
    sessions = SessionRepository(database)
    preferences = PreferenceRepository(database)
    memories = MemoryRepository(database)
    session = sessions.create()
    memories.create("project", "主项目记忆", "user")
    memories.create("lesson", "关联经验记忆", "user")
    with database.connection() as connection:
        connection.execute("UPDATE memories SET memory_id = 'seed' WHERE content = '主项目记忆'")
        connection.execute("UPDATE memories SET memory_id = 'related' WHERE content = '关联经验记忆'")
    graph = FakeGraphAdapter()

    result = ContextBuilder(
        sessions,
        preferences,
        memories,
        retriever=FixedRetriever(),
        graph_adapter=graph,
        memory_limit=2,
    ).build(session["session_id"], "当前任务")

    assert result.memory_count == 2
    assert "主项目记忆" in result.messages[0].content
    assert "关联经验记忆" in result.messages[0].content
    assert graph.calls == [(["seed"], 1)]


class FailingRetriever:
    """模拟外部向量服务不可用。"""

    def search(self, query: str, *, threshold: float, limit: int):
        raise RuntimeError("vector service unavailable")


class FailingGraphAdapter(FakeGraphAdapter):
    """模拟 Neo4j 不可用。"""

    def expand(self, seed_memory_ids: tuple[str, ...], *, limit: int):
        raise RuntimeError("graph service unavailable")


def test_external_retrieval_failures_fall_back_without_blocking_task(tmp_path: Path) -> None:
    database = Database(tmp_path / "fallback.sqlite3")
    database.initialize()
    sessions = SessionRepository(database)
    preferences = PreferenceRepository(database)
    memories = MemoryRepository(database)
    session = sessions.create()
    memories.create("project", "项目使用 pytest", "user")

    result = ContextBuilder(
        sessions,
        preferences,
        memories,
        retriever=FailingRetriever(),
        graph_adapter=FailingGraphAdapter(),
        memory_threshold=0.4,
    ).build(session["session_id"], "请说明项目 pytest 测试方式")

    assert result.memory_count == 1
    assert "项目使用 pytest" in result.messages[0].content
