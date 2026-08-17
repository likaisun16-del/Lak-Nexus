"""审计仓储：记录每次工具调用和审批决定，参数与摘要进入数据库前已脱敏。"""

from __future__ import annotations

import uuid
from typing import Any

from .database import Database
from .task_repository import utc_now


class AuditRepository:
    """工具调用和审批表的持久化入口。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_tool_call(
        self, task_id: str, tool_call_id: str, tool_name: str, arguments_redacted: str
    ) -> None:
        """记录工具调用开始，未知工具也必须先留下审计记录。"""

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls(
                    tool_call_id, task_id, tool_name, arguments_redacted, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tool_call_id, task_id, tool_name, arguments_redacted, "running", utc_now()),
            )

    def finish_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        result_summary: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """记录成功、失败、拒绝或取消及其安全摘要。"""

        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_calls
                SET status = ?, finished_at = ?, result_summary = ?, error_type = ?, error_message = ?
                WHERE tool_call_id = ?
                """,
                (
                    status,
                    utc_now(),
                    result_summary,
                    error_type,
                    error_message,
                    tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"工具审计更新失败：调用记录不存在：{tool_call_id}")

    def record_approval(
        self,
        task_id: str,
        tool_call_id: str,
        action_type: str,
        request_summary: str,
        decision: bool,
    ) -> None:
        """保存人工审批结果，审批摘要不包含完整文件内容或命令输出。"""

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals(
                    approval_id, task_id, tool_call_id, action_type,
                    request_summary, decision, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    tool_call_id,
                    action_type,
                    request_summary,
                    "approved" if decision else "denied",
                    utc_now(),
                ),
            )

    def list_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        """读取任务的工具审计，便于集成测试和人工诊断。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY started_at, tool_call_id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
