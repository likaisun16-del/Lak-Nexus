"""Session 与可见消息树仓储：负责 SQLite 外键、路径校验、分支切换和事务。"""

from __future__ import annotations

import uuid
from typing import Any

from ..errors import SessionError
from ..safety.redaction import redact_text
from .database import Database
from .task_repository import utc_now

DEFAULT_SESSION_TITLE = "新会话"
_VISIBLE_ROLES = frozenset({"user", "assistant"})


class SessionRepository:
    """以自引用 parent_message_id 保存树形可见消息，并维护活动叶子。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, session_id: str | None = None, title: str = DEFAULT_SESSION_TITLE) -> dict[str, Any]:
        """创建空会话并返回完整记录。"""

        session_id = session_id or uuid.uuid4().hex
        title = self._safe_title(title)
        now = utc_now()
        with self.database.connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO sessions(session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (session_id, title, now, now),
                )
            except Exception as exc:
                raise SessionError(
                    f"会话创建失败：无法写入 Session {session_id}，原因：{type(exc).__name__}"
                ) from exc
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> dict[str, Any] | None:
        """按稳定 ID 查询会话。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        """按最近消息时间和稳定 ID 确定性倒序列出会话。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, session_id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_title(self, session_id: str, title: str) -> None:
        """更新标题；空标题会被拒绝，避免覆盖为不可见值。"""

        safe_title = self._safe_title(title)
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (safe_title, utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise SessionError(f"会话标题更新失败：Session 不存在：{session_id}")

    def delete(self, session_id: str) -> bool:
        """在单一事务内删除 Session 及其消息树，任务和审计由外键保留。"""

        with self.database.connection() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return cursor.rowcount == 1

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        parent_message_id: str | None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """校验同会话父节点后写入可见消息，并把新消息设为活动叶子。"""

        if role not in _VISIBLE_ROLES:
            raise SessionError(f"消息写入失败：不允许保存内部角色：{role}")
        safe_content = redact_text(content)
        message_id = uuid.uuid4().hex
        now = utc_now()
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise SessionError(f"消息写入失败：Session 不存在：{session_id}")
            if parent_message_id is None:
                existing = connection.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)
                ).fetchone()
                if existing is not None:
                    raise SessionError(
                        f"消息写入失败：非空 Session 的消息必须指定同会话父节点：{session_id}"
                    )
            else:
                parent = connection.execute(
                    "SELECT session_id FROM messages WHERE message_id = ?", (parent_message_id,)
                ).fetchone()
                if parent is None:
                    raise SessionError(f"消息写入失败：父消息不存在：{parent_message_id}")
                if parent[0] != session_id:
                    raise SessionError(
                        f"消息写入失败：父消息属于其他 Session：{parent_message_id}"
                    )
            if task_id is not None:
                task = connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise SessionError(f"消息写入失败：关联 Task 不存在：{task_id}")
            connection.execute(
                "INSERT INTO messages(message_id, session_id, role, content, parent_message_id, "
                "task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, session_id, role, safe_content, parent_message_id, task_id, now),
            )
            connection.execute(
                "UPDATE sessions SET active_leaf_id = ?, updated_at = ? WHERE session_id = ?",
                (message_id, now, session_id),
            )
        return self.get_message(message_id)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """查询一条可见消息。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_session_for_message(self, message_id: str) -> str | None:
        """返回消息所属 Session，供 continue-from 和 commit 查询使用。"""

        message = self.get_message(message_id)
        return message["session_id"] if message else None

    def current_path(self, session_id: str) -> list[dict[str, Any]]:
        """读取活动叶子到根的父链，并反转成模型可见的正序。"""

        session = self._require_session(session_id)
        leaf_id = session["active_leaf_id"]
        if leaf_id is None:
            return []
        return self.path_to_message(session_id, leaf_id)

    def path_to_message(self, session_id: str, message_id: str) -> list[dict[str, Any]]:
        """读取指定消息到根的路径，拒绝跨 Session 消息和环。"""

        self._require_session(session_id)
        path: list[dict[str, Any]] = []
        visited: set[str] = set()
        current_id: str | None = message_id
        with self.database.connection() as connection:
            while current_id is not None:
                if current_id in visited:
                    raise SessionError(f"消息路径读取失败：检测到父链循环：{current_id}")
                visited.add(current_id)
                row = connection.execute(
                    "SELECT * FROM messages WHERE message_id = ?", (current_id,)
                ).fetchone()
                if row is None:
                    raise SessionError(f"消息路径读取失败：消息不存在：{current_id}")
                if row["session_id"] != session_id:
                    raise SessionError(
                        f"消息路径读取失败：消息属于其他 Session：{current_id}"
                    )
                item = dict(row)
                path.append(item)
                current_id = row["parent_message_id"]
        path.reverse()
        return path

    def set_active_leaf(self, session_id: str, message_id: str) -> None:
        """切换活动叶子，只更新 Session 指针，不删除任何旧分支。"""

        message = self.get_message(message_id)
        if message is None:
            raise SessionError(f"活动分支切换失败：消息不存在：{message_id}")
        if message["session_id"] != session_id:
            raise SessionError(f"活动分支切换失败：消息属于其他 Session：{message_id}")
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE sessions SET active_leaf_id = ?, updated_at = ? WHERE session_id = ?",
                (message_id, utc_now(), session_id),
            )

    def list_children(self, message_id: str) -> list[dict[str, Any]]:
        """按创建顺序列出一个消息的直接子节点。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE parent_message_id = ? ORDER BY created_at, rowid",
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_branches(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        """返回分支点及叶子摘要，避免把完整树复制到展示层。"""

        session = self._require_session(session_id)
        with self.database.connection() as connection:
            branch_rows = connection.execute(
                "SELECT m.*, COUNT(child.message_id) AS child_count "
                "FROM messages AS m JOIN messages AS child "
                "ON child.parent_message_id = m.message_id "
                "WHERE m.session_id = ? GROUP BY m.message_id "
                "HAVING COUNT(child.message_id) > 1 ORDER BY m.created_at, m.rowid",
                (session_id,),
            ).fetchall()
            leaf_rows = connection.execute(
                "SELECT m.* FROM messages AS m LEFT JOIN messages AS child "
                "ON child.parent_message_id = m.message_id "
                "WHERE m.session_id = ? AND child.message_id IS NULL "
                "ORDER BY m.created_at, m.rowid",
                (session_id,),
            ).fetchall()
        active_leaf_id = session["active_leaf_id"]
        branches = [dict(row) for row in branch_rows]
        leaves = [dict(row) for row in leaf_rows]
        for leaf in leaves:
            leaf["is_active"] = leaf["message_id"] == active_leaf_id
        return {"branch_points": branches, "leaves": leaves}

    def assistant_count(self, session_id: str) -> int:
        """统计已保存的可见 assistant 最终消息数量。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'assistant'",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if session is None:
            raise SessionError(f"Session 操作失败：会话不存在：{session_id}")
        return session

    @staticmethod
    def _safe_title(title: str) -> str:
        safe_title = redact_text(str(title)).strip().replace("\n", " ")
        if not safe_title:
            raise SessionError("会话标题操作失败：标题不能为空")
        return safe_title[:80]
