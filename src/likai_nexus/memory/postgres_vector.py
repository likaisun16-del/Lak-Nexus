"""pgvector 索引适配器：只保存可重建向量，不替代 PostgreSQL 记忆正文。"""

from __future__ import annotations

import math
from collections.abc import Sequence

from ..errors import ValidationError
from ..storage.memory_repository import MemoryRepository
from ..storage.postgres import PostgresDatabase
from .contracts import MemoryCandidate, VectorIndex
from .retrieval_adapters import VectorMemoryRetriever


class PostgresVectorIndex:
    """基于 pgvector 的最小向量索引，默认不改变 SQLite Runtime。"""

    def __init__(self, database: PostgresDatabase, dimension: int) -> None:
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise ValidationError(f"pgvector 配置失败：dimension 必须是正整数：{dimension}")
        self.database = database
        self.dimension = dimension

    def initialize(self) -> None:
        """创建 pgvector 扩展和记忆索引表；不保存完整记忆正文。"""

        with self.database.connection() as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_embeddings ("
                "memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE, "
                "content_hash TEXT NOT NULL, "
                f"embedding vector({self.dimension}) NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )

    def upsert(self, memory_id: str, content_hash: str, embedding: Sequence[float]) -> None:
        vector = self._vector_literal(embedding)
        with self.database.connection() as connection:
            connection.execute(
                "INSERT INTO memory_embeddings(memory_id, content_hash, embedding, updated_at) "
                "VALUES (?, ?, ?::vector, CURRENT_TIMESTAMP::text) "
                "ON CONFLICT(memory_id) DO UPDATE SET content_hash = EXCLUDED.content_hash, "
                "embedding = EXCLUDED.embedding, updated_at = CURRENT_TIMESTAMP::text",
                (memory_id, content_hash, vector),
            )

    def delete(self, memory_id: str) -> None:
        with self.database.connection() as connection:
            connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))

    def search(
        self,
        embedding: Sequence[float],
        *,
        threshold: float,
        limit: int,
    ) -> tuple[MemoryCandidate, ...]:
        vector = self._vector_literal(embedding)
        if not 0.0 <= threshold <= 1.0:
            raise ValidationError(f"pgvector 检索失败：threshold 超出范围：{threshold}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError(f"pgvector 检索失败：limit 必须是正整数：{limit}")
        query = (
            "WITH query AS (SELECT ?::vector AS embedding) "
            "SELECT e.memory_id, 1 - (e.embedding <=> query.embedding) AS score "
            "FROM memory_embeddings AS e JOIN memories AS m ON m.memory_id = e.memory_id "
            "CROSS JOIN query WHERE m.status = 'active' "
            "AND 1 - (e.embedding <=> query.embedding) >= ? "
            "ORDER BY e.embedding <=> query.embedding, e.memory_id LIMIT ?"
        )
        with self.database.connection() as connection:
            rows = connection.execute(query, (vector, threshold, limit)).fetchall()
        return tuple(MemoryCandidate(row["memory_id"], float(row["score"])) for row in rows)

    def _vector_literal(self, embedding: Sequence[float]) -> str:
        if len(embedding) != self.dimension:
            raise ValidationError(
                f"pgvector 校验失败：维度不匹配，期望 {self.dimension}，实际 {len(embedding)}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in embedding
        ):
            raise ValidationError("pgvector 校验失败：向量必须只包含有限数字")
        return "[" + ",".join(str(float(value)) for value in embedding) + "]"


def as_vector_index(index: PostgresVectorIndex) -> VectorIndex:
    """返回 ContextBuilder 可注入的向量索引契约。"""

    return index


def backfill_pending_memories(
    repository: MemoryRepository,
    retriever: VectorMemoryRetriever,
    *,
    limit: int = 100,
) -> dict[str, int]:
    """批量建立待索引记忆，并把索引状态写回权威存储。"""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValidationError(f"记忆索引回填失败：limit 必须是正整数：{limit}")
    ready = 0
    failed = 0
    for memory in repository.list_pending_embeddings(limit):
        try:
            retriever.upsert(memory["memory_id"], memory["content_hash"], memory["content"])
        except Exception:  # noqa: BLE001
            repository.set_embedding_status(memory["memory_id"], "failed")
            failed += 1
        else:
            repository.set_embedding_status(memory["memory_id"], "ready")
            ready += 1
    return {"ready": ready, "failed": failed}
