"""存储层公共契约：让 ContextBuilder 与 SQLite、PostgreSQL 仓储实现解耦。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class SessionContextStore(Protocol):
    """提供当前 Session 活动分支的可见消息。"""

    def current_path(self, session_id: str) -> list[dict[str, Any]]:
        """返回从根到活动叶子的可见消息。"""


class PreferenceStore(Protocol):
    """提供当前生效的用户偏好。"""

    def list(self) -> list[dict[str, Any]]:
        """返回可安全注入上下文的偏好记录。"""


class MemoryStore(Protocol):
    """提供长期记忆正文的权威回表读取。"""

    def list_active(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回活动记忆候选。"""

    def find_by_ids(self, memory_ids: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """按检索结果 ID 返回活动记忆正文。"""


class StorageBackend(Protocol):
    """未来 PostgreSQL 适配器需要提供的上下文存储集合。"""

    sessions: SessionContextStore
    preferences: PreferenceStore
    memories: MemoryStore


def validate_memory_rows(rows: Sequence[Mapping[str, object]]) -> None:
    """校验外部存储适配器返回的最小记忆字段，避免错误数据进入上下文。"""

    required = {"memory_id", "content", "importance"}
    for index, row in enumerate(rows):
        missing = required - row.keys()
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"存储适配器返回失败：第 {index} 条记忆缺少字段：{missing_text}")
