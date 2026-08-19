"""Task 与 Git Commit 关联仓储：只保存完整版本标识，不执行版本控制操作。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from ..errors import SessionError
from .database import Database
from .task_repository import utc_now

_FULL_COMMIT_SHA = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


class CommitRepository:
    """持久化一个 Task 至多一个 Commit 关联。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, task_id: str, commit_sha: str, repository_path: str | None = None) -> dict[str, Any]:
        """写入完整 SHA；同一 Task 已有不同版本时拒绝覆盖。"""

        if not _FULL_COMMIT_SHA.fullmatch(commit_sha):
            raise SessionError("Git 版本关联失败：只允许保存完整 40 或 64 位 Commit SHA")
        normalized_sha = commit_sha.lower()
        with self.database.connection() as connection:
            task = connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise SessionError(f"Git 版本关联失败：Task 不存在：{task_id}")
            existing = connection.execute(
                "SELECT * FROM task_commits WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if existing["commit_sha"] != normalized_sha:
                    raise SessionError(f"Git 版本关联失败：Task 已关联其他 Commit：{task_id}")
                return dict(existing)
            connection.execute(
                "INSERT INTO task_commits(association_id, task_id, commit_sha, repository_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, normalized_sha, repository_path, utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM task_commits WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row)  # type: ignore[arg-type]

    def get_for_task(self, task_id: str) -> dict[str, Any] | None:
        """查询 Task 的 Commit 关联，不改变 Task 状态。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM task_commits WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_for_message(self, message_id: str) -> dict[str, Any] | None:
        """通过 assistant Message 的 Task 外键查询 Commit。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT c.* FROM messages AS m JOIN task_commits AS c ON c.task_id = m.task_id "
                "WHERE m.message_id = ? AND m.role = 'assistant'",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None
