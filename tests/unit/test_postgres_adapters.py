"""PostgreSQL/pgvector 适配器测试：使用 DB-API 替身验证迁移和查询契约。"""

from __future__ import annotations

from likai_nexus.memory.postgres_vector import PostgresVectorIndex
from likai_nexus.memory.retrieval_adapters import VectorMemoryRetriever
from likai_nexus.orchestrator.schemas import TaskStatus
from likai_nexus.runtime import build_postgres_context_builder
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.postgres import PostgresDatabase, restore_postgres_snapshot
from likai_nexus.storage.snapshot import PORTABLE_TABLES, PortableSnapshot
from likai_nexus.storage.task_repository import TaskRepository


class RecordingCursor:
    """记录 SQL 并返回可控结果的 DB-API Cursor 替身。"""

    def __init__(self, owner: RecordingConnection) -> None:
        self.owner = owner
        self.description: tuple[tuple[str], ...] | None = None
        self.rows: list[tuple[object, ...]] = []
        self.rowcount = 1

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.owner.statements.append((statement, parameters))
        if "SELECT e.memory_id" in statement:
            self.description = (("memory_id",), ("score",))
            self.rows = [("memory-1", 0.91)]
        elif statement.lstrip().upper().startswith("SELECT 1 FROM"):
            self.description = (("exists",),)
            self.rows = []
        else:
            self.description = None
            self.rows = []

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def close(self) -> None:
        return None


class RecordingConnection:
    """可重复创建的 DB-API Connection 替身。"""

    def __init__(self, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        self.statements = statements
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class RecordingFactory:
    """为每个事务创建独立连接，同时保留全部 SQL 记录。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.connections: list[RecordingConnection] = []

    def __call__(self) -> RecordingConnection:
        connection = RecordingConnection(self.statements)
        self.connections.append(connection)
        return connection


def _database() -> tuple[PostgresDatabase, RecordingFactory]:
    factory = RecordingFactory()
    return PostgresDatabase(factory), factory


def _snapshot() -> PortableSnapshot:
    tables = {table: () for table in PORTABLE_TABLES}
    tables["tasks"] = (
        {
            "task_id": "task-1",
            "request_text": "迁移验证",
            "review_mode": "strict",
            "status": TaskStatus.SUCCESS.value,
            "created_at": "2026-08-20T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "result_summary": None,
            "error_type": None,
            "error_message": None,
        },
    )
    tables["sessions"] = (
        {
            "session_id": "session-1",
            "title": "迁移测试",
            "created_at": "2026-08-20T00:00:00+00:00",
            "updated_at": "2026-08-20T00:00:00+00:00",
            "last_message_at": "2026-08-20T00:00:00+00:00",
            "active_leaf_id": "message-2",
        },
    )
    tables["messages"] = (
        {
            "message_id": "message-1",
            "session_id": "session-1",
            "role": "user",
            "content": "迁移测试",
            "parent_message_id": None,
            "task_id": "task-1",
            "execution_status": "success",
            "retry_of_message_id": None,
            "version_reason": None,
            "created_at": "2026-08-20T00:00:00+00:00",
        },
        {
            "message_id": "message-2",
            "session_id": "session-1",
            "role": "assistant",
            "content": "已迁移",
            "parent_message_id": "message-1",
            "task_id": "task-1",
            "execution_status": "success",
            "retry_of_message_id": None,
            "version_reason": None,
            "created_at": "2026-08-20T00:00:01+00:00",
        },
    )
    return PortableSnapshot(schema_version=1, tables=tables)


def test_postgres_database_translates_existing_repository_sql() -> None:
    database, factory = _database()
    database.initialize()

    assert TaskRepository(database).create("task-1", "创建任务", ReviewMode.STRICT)
    task_insert = next(statement for statement, _ in factory.statements if "INSERT INTO tasks" in statement)
    assert "INSERT OR IGNORE" not in task_insert
    assert "ON CONFLICT DO NOTHING" in task_insert
    assert "%s" in task_insert


def test_postgres_snapshot_restore_uses_portable_table_order() -> None:
    database, factory = _database()

    counts = restore_postgres_snapshot(database, _snapshot())

    assert counts["tasks"] == 1
    task_insert = next(i for i, (statement, _) in enumerate(factory.statements) if "INSERT INTO tasks" in statement)
    message_insert = next(
        i for i, (statement, _) in enumerate(factory.statements) if "INSERT INTO messages" in statement
    )
    assert task_insert < message_insert
    assert any("UPDATE messages SET parent_message_id" in statement for statement, _ in factory.statements)
    assert any("UPDATE sessions SET active_leaf_id" in statement for statement, _ in factory.statements)


def test_pgvector_index_initializes_and_returns_memory_candidates() -> None:
    database, factory = _database()
    index = PostgresVectorIndex(database, dimension=2)

    index.initialize()
    index.upsert("memory-1", "hash-1", (0.1, 0.2))
    candidates = index.search((0.1, 0.2), threshold=0.7, limit=5)
    index.delete("memory-1")

    assert candidates[0].memory_id == "memory-1"
    assert candidates[0].score == 0.91
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in statement for statement, _ in factory.statements)
    upsert = next(statement for statement, _ in factory.statements if "INSERT INTO memory_embeddings" in statement)
    assert "::vector" in upsert
    search = next(statement for statement, _ in factory.statements if "SELECT e.memory_id" in statement)
    assert "<=>" in search


class FixedEmbedder:
    """验证 Runtime 组装的离线 EmbeddingProvider 替身。"""

    dimension = 2

    def embed(self, text: str) -> tuple[float, float]:
        del text
        return (0.1, 0.2)


def test_runtime_can_explicitly_assemble_postgres_vector_context() -> None:
    database, builder = build_postgres_context_builder(
        RecordingFactory(), embedding_provider=FixedEmbedder()
    )

    assert database.dialect == "postgresql"
    assert isinstance(builder.retriever, VectorMemoryRetriever)
