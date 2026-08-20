"""长期记忆 SQLite 仓储：负责安全校验、来源追踪、去重和可重建索引状态。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from ..errors import ValidationError
from ..safety.redaction import content_sha256, redact_text
from .database import Database
from .task_repository import utc_now

_MEMORY_TYPES = frozenset({"fact", "project", "lesson"})
_SOURCE_TYPES = frozenset({"user", "conversation", "task", "system"})
_STATUSES = frozenset({"active", "disabled"})
_EMBEDDING_STATUSES = frozenset({"pending", "ready", "failed"})
_SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_MAX_CONTENT_BYTES = 16 * 1024


class MemoryRepository:
    """提供长期记忆的最小 CRUD，不依赖外部向量或图数据库。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        memory_type: str,
        content: str,
        source_type: str,
        source_ref: str | None = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        """创建活动记忆；相同活动内容直接返回已有记录，保证幂等去重。"""

        self._validate_type(memory_type, _MEMORY_TYPES, "记忆类型")
        self._validate_type(source_type, _SOURCE_TYPES, "来源类型")
        safe_content = self._validate_content(content)
        safe_ref = self._validate_source_ref(source_type, source_ref)
        self._validate_importance(importance)
        content_hash = content_sha256(safe_content)
        self._validate_source_exists(source_type, safe_ref)
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT memory_id FROM memories WHERE content_hash = ? AND status = 'active'",
                (content_hash,),
            ).fetchone()
            if existing is not None:
                return self.get(existing[0])  # type: ignore[return-value]
            memory_id = uuid.uuid4().hex
            now = utc_now()
            connection.execute(
                "INSERT INTO memories(memory_id, memory_type, content, source_type, source_ref, "
                "status, importance, content_hash, embedding_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 'pending', ?, ?)",
                (
                    memory_id,
                    memory_type,
                    safe_content,
                    source_type,
                    safe_ref,
                    importance,
                    content_hash,
                    now,
                    now,
                ),
            )
        return self.get(memory_id)  # type: ignore[return-value]

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """按稳定 ID 查询记忆。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_active(self, limit: int = 100) -> list[dict[str, Any]]:
        """按重要性和更新时间返回活动记忆。"""

        self._validate_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                "ORDER BY importance DESC, updated_at DESC, memory_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_by_source(self, source_type: str, source_ref: str) -> list[dict[str, Any]]:
        """按来源查询记忆，供消息或任务详情回溯。"""

        self._validate_type(source_type, _SOURCE_TYPES, "来源类型")
        safe_ref = self._validate_source_ref(source_type, source_ref)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memories WHERE source_type = ? AND source_ref = ? "
                "ORDER BY created_at, memory_id",
                (source_type, safe_ref),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_by_ids(self, memory_ids: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """按给定顺序读取活动记忆，供未来向量或图索引回表。"""

        self._validate_limit(limit)
        unique_ids = list(dict.fromkeys(memory_ids))[:limit]
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM memories WHERE status = 'active' AND memory_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        by_id = {row["memory_id"]: dict(row) for row in rows}
        return [by_id[memory_id] for memory_id in unique_ids if memory_id in by_id]

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
    ) -> dict[str, Any]:
        """更新正文或重要性；正文变化时重置向量索引状态。"""

        current = self.get(memory_id)
        if current is None:
            raise ValidationError(f"记忆更新失败：记忆不存在：{memory_id}")
        if content is None and importance is None:
            return current
        safe_content = current["content"] if content is None else self._validate_content(content)
        safe_importance = current["importance"] if importance is None else importance
        self._validate_importance(safe_importance)
        content_hash = content_sha256(safe_content)
        with self.database.connection() as connection:
            duplicate = connection.execute(
                "SELECT memory_id FROM memories WHERE content_hash = ? "
                "AND status = 'active' AND memory_id <> ?",
                (content_hash, memory_id),
            ).fetchone()
            if duplicate is not None:
                raise ValidationError(
                    f"记忆更新失败：内容与活动记忆重复：{duplicate[0]}"
                )
            connection.execute(
                "UPDATE memories SET content = ?, importance = ?, content_hash = ?, "
                "embedding_status = ?, updated_at = ? WHERE memory_id = ?",
                (
                    safe_content,
                    safe_importance,
                    content_hash,
                    "pending" if content is not None else current["embedding_status"],
                    utc_now(),
                    memory_id,
                ),
            )
        return self.get(memory_id)  # type: ignore[return-value]

    def disable(self, memory_id: str) -> bool:
        """禁用记忆但保留其来源和历史字段。"""

        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE memories SET status = 'disabled', updated_at = ? "
                "WHERE memory_id = ? AND status = 'active'",
                (utc_now(), memory_id),
            )
            return cursor.rowcount == 1

    def set_embedding_status(self, memory_id: str, status: str) -> None:
        """更新可重建向量索引状态，不保存向量正文。"""

        self._validate_type(status, _EMBEDDING_STATUSES, "向量索引状态")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE memories SET embedding_status = ?, updated_at = ? WHERE memory_id = ?",
                (status, utc_now(), memory_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError(f"向量状态更新失败：记忆不存在：{memory_id}")

    def _validate_source_exists(self, source_type: str, source_ref: str | None) -> None:
        if source_type not in {"conversation", "task"}:
            return
        table = "messages" if source_type == "conversation" else "tasks"
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE { 'message_id' if table == 'messages' else 'task_id' } = ?",
                (source_ref,),
            ).fetchone()
        if row is None:
            raise ValidationError(
                f"记忆来源校验失败：{source_type} 来源不存在：{source_ref}"
            )

    @staticmethod
    def _validate_content(content: str) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("记忆保存失败：content 必须是非空字符串")
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValidationError(
                f"记忆保存失败：content 超过 {_MAX_CONTENT_BYTES} 字节限制"
            )
        safe_content = redact_text(content)
        if safe_content != content:
            raise ValidationError("记忆保存失败：content 可能包含 Token、密码或私钥内容")
        return content

    @staticmethod
    def _validate_source_ref(source_type: str, source_ref: str | None) -> str | None:
        if source_type in {"conversation", "task"} and not source_ref:
            raise ValidationError(f"记忆来源校验失败：{source_type} 必须提供 source_ref")
        if source_ref is not None and (
            not isinstance(source_ref, str) or not _SOURCE_REF_PATTERN.fullmatch(source_ref)
        ):
            raise ValidationError(f"记忆来源校验失败：source_ref 格式无效：{source_ref!r}")
        return source_ref

    @staticmethod
    def _validate_importance(importance: float) -> None:
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            raise ValidationError("记忆校验失败：importance 必须是 0 到 1 的数字")
        if not 0.0 <= importance <= 1.0:
            raise ValidationError(f"记忆校验失败：importance 超出范围：{importance}")

    @staticmethod
    def _validate_type(value: str, allowed: frozenset[str], label: str) -> None:
        if not isinstance(value, str) or value not in allowed:
            values = ", ".join(sorted(allowed))
            raise ValidationError(f"{label}校验失败：{value!r} 不允许，允许值为：{values}")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError(f"记忆查询失败：limit 必须是正整数，实际值为：{limit}")
