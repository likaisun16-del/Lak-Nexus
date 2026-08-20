"""向量检索适配器：把文本向量化和索引查询组合为 ContextBuilder 可用的检索器。"""

from __future__ import annotations

import math
from collections.abc import Sequence

from ..errors import ValidationError
from .contracts import EmbeddingProvider, MemoryCandidate, MemoryRetriever, VectorIndex


class VectorMemoryRetriever:
    """向量检索编排器，不绑定具体模型或向量数据库供应商。"""

    def __init__(self, embedder: EmbeddingProvider, index: VectorIndex) -> None:
        self.embedder = embedder
        self.index = index

    def search(
        self, query: str, *, threshold: float, limit: int
    ) -> Sequence[MemoryCandidate]:
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("向量检索失败：query 必须是非空字符串")
        if not 0.0 <= threshold <= 1.0:
            raise ValidationError(f"向量检索失败：threshold 超出范围：{threshold}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError(f"向量检索失败：limit 必须是正整数：{limit}")
        embedding = tuple(self.embedder.embed(query))
        self._validate_embedding(embedding)
        return tuple(self.index.search(embedding, threshold=threshold, limit=limit))

    def upsert(self, memory_id: str, content_hash: str, content: str) -> None:
        """生成正文向量并写入可重建索引，正文仍由 SQLite/PostgreSQL 保存。"""

        if not memory_id or not content_hash:
            raise ValidationError("向量索引写入失败：memory_id 和 content_hash 不能为空")
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("向量索引写入失败：content 必须是非空字符串")
        embedding = tuple(self.embedder.embed(content))
        self._validate_embedding(embedding)
        self.index.upsert(memory_id, content_hash, embedding)

    def delete(self, memory_id: str) -> None:
        """删除可重建索引中的记忆点。"""

        if not memory_id:
            raise ValidationError("向量索引删除失败：memory_id 不能为空")
        self.index.delete(memory_id)

    def as_retriever(self) -> MemoryRetriever:
        """返回供 ContextBuilder 注入的最小检索契约。"""

        return self

    def _validate_embedding(self, embedding: Sequence[float]) -> None:
        dimension = self.embedder.dimension
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise ValidationError(f"向量校验失败：EmbeddingProvider.dimension 无效：{dimension}")
        if len(embedding) != dimension:
            raise ValidationError(
                f"向量校验失败：维度不匹配，期望 {dimension}，实际 {len(embedding)}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in embedding
        ):
            raise ValidationError("向量校验失败：向量必须只包含有限数字")
