"""任务仓储：为 AgentLoop 维护 pending、running、success、failed、cancelled 状态及安全摘要。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..orchestrator.schemas import TaskStatus
from ..safety.redaction import audit_text_summary, redact_text
from .database import Database


def utc_now() -> str:
    """返回统一的 UTC ISO 时间，便于跨进程审计排序。"""

    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskRepository:
    """任务表的最小 CRUD 和启动恢复操作。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, task_id: str, request_text: str) -> bool:
        """创建任务；重复 ID 返回 False，绝不覆盖已有任务。"""

        request_summary = audit_text_summary("任务请求", request_text)
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tasks(task_id, request_text, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, request_summary, TaskStatus.PENDING.value, utc_now()),
            )
            return cursor.rowcount == 1

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_summary: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新状态和对应摘要，错误字段只保存脱敏后的诊断信息。"""

        with self.database.connection() as connection:
            current = connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if current is None:
            raise KeyError(f"任务状态更新失败：任务不存在：{task_id}")
        current_status = TaskStatus(current[0])
        if not self._can_transition(current_status, status):
            raise ValueError(
                f"任务状态更新失败：不允许从 {current_status.value} 转为 {status.value}：{task_id}"
            )

        updates: dict[str, Any] = {"status": status.value}
        if status is TaskStatus.RUNNING:
            updates["started_at"] = utc_now()
        if status in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            updates["finished_at"] = utc_now()
        if result_summary is not None:
            updates["result_summary"] = audit_text_summary("任务结果", result_summary)
        if error_type is not None:
            updates["error_type"] = error_type
        if error_message is not None:
            updates["error_message"] = redact_text(error_message)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [task_id]
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"任务状态更新失败：任务不存在：{task_id}")

    @staticmethod
    def _can_transition(current: TaskStatus, target: TaskStatus) -> bool:
        """限制任务状态单向流转，终态不能重新进入 running。"""

        if current is target:
            return True
        allowed = {
            TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED},
            TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED},
            TaskStatus.SUCCESS: set(),
            TaskStatus.FAILED: set(),
            TaskStatus.CANCELLED: set(),
        }
        return target in allowed[current]

    def get(self, task_id: str) -> dict[str, Any] | None:
        """读取任务记录，供 CLI、测试和恢复流程诊断。"""

        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def recover_running(self) -> int:
        """把进程重启时遗留的 running 任务标记为可诊断失败。"""

        message = "程序启动恢复：任务此前处于 running，推断执行进程已中断"
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, error_type = ?, error_message = ?
                WHERE status = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    utc_now(),
                    "InterruptedTask",
                    message,
                    TaskStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount
