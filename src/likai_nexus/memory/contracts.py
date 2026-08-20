"""记忆检索公共契约：隔离向量索引、图索引与当前 SQLite 召回实现。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """检索器返回的记忆 ID 和相似度，正文仍必须回权威存储读取。"""

    memory_id: str
    score: float


class MemoryRetriever(Protocol):
    """按文本查询返回超过阈值的长期记忆 ID。"""

    def search(
        self, query: str, *, threshold: float, limit: int
    ) -> Sequence[MemoryCandidate]:
        """返回候选 ID，不直接返回权威正文。"""


class EmbeddingProvider(Protocol):
    """文本向量化接口，具体模型由后续部署选择。"""

    @property
    def dimension(self) -> int:
        """返回向量维度。"""

    def embed(self, text: str) -> Sequence[float]:
        """把文本转换为向量。"""


class VectorIndex(Protocol):
    """可替换向量数据库索引接口，不保存权威记忆正文。"""

    def upsert(
        self,
        memory_id: str,
        content_hash: str,
        embedding: Sequence[float],
    ) -> None:
        """写入或更新可重建的向量索引。"""

    def delete(self, memory_id: str) -> None:
        """删除或标记删除一个索引点。"""

    def search(
        self,
        embedding: Sequence[float],
        *,
        threshold: float,
        limit: int,
    ) -> Sequence[MemoryCandidate]:
        """按向量相似度返回记忆 ID。"""


class GraphMemoryAdapter(Protocol):
    """Neo4j 等图存储的最小投影与一跳/两跳召回接口。"""

    def upsert_memory(
        self,
        memory_id: str,
        memory_type: str,
        source_type: str,
        source_ref: str | None,
        content_hash: str,
    ) -> None:
        """写入可重建的记忆节点及其来源关系。"""

    def disable_memory(self, memory_id: str) -> None:
        """移除或禁用图中的记忆投影。"""

    def expand(
        self, seed_memory_ids: Sequence[str], *, limit: int
    ) -> Sequence[MemoryCandidate]:
        """从向量召回的种子扩展关联记忆 ID。"""
