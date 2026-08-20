"""审计仓储：为 ToolExecutor 持久化工具调用和审批决定，入库参数与摘要必须已脱敏。"""

from __future__ import annotations

import uuid
from typing import Any

from ..safety.redaction import redact_text, safe_audit_identifier
from .database import Database
from .task_repository import utc_now


class AuditRepository:
    """工具调用和审批表的持久化入口。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_tool_call(
        self,
        task_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_redacted: str,
        *,
        canonical_tool_name: bool = False,
    ) -> str:
        """记录工具调用开始，未知工具也必须先留下审计记录。"""

        audit_id = uuid.uuid4().hex
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls(
                    audit_id, task_id, tool_name, tool_call_id, arguments_redacted, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    task_id,
                    safe_audit_identifier(tool_name, "tool", trusted=canonical_tool_name),
                    safe_audit_identifier(tool_call_id, "call"),
                    redact_text(arguments_redacted),
                    "running",
                    utc_now(),
                ),
            )
        return audit_id

    def finish_tool_call(
        self,
        audit_id: str,
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
                WHERE audit_id = ?
                """,
                (
                    status,
                    utc_now(),
                    redact_text(result_summary) if result_summary is not None else None,
                    safe_audit_identifier(error_type, "error", trusted=True) if error_type else None,
                    redact_text(error_message) if error_message is not None else None,
                    audit_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"工具审计更新失败：审计记录不存在：{audit_id}")

    def record_approval(
        self,
        task_id: str,
        tool_call_id: str,
        action_type: str,
        request_summary: str,
        decision: bool,
        decision_source: str = "human",
    ) -> None:
        """保存人工或模式审批结果，摘要不包含完整文件内容或命令输出。"""

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals(
                    approval_id, task_id, tool_call_id, action_type,
                    request_summary, decision, decision_source, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    safe_audit_identifier(tool_call_id, "call"),
                    safe_audit_identifier(action_type, "action", trusted=True),
                    redact_text(request_summary),
                    "approved" if decision else "denied",
                    decision_source,
                    utc_now(),
                ),
            )

    def list_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        """读取任务的工具审计，便于集成测试和人工诊断。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_approvals(self, task_id: str) -> list[dict[str, Any]]:
        """读取任务的审批及其来源，便于验证人工与模式自动决定。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_successful_code_mutation(self, task_id: str) -> bool:
        """判断任务是否成功调用了明确的代码写入工具，作为版本关联资格依据。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM tool_calls "
                "WHERE task_id = ? AND tool_name IN ('write', 'edit') AND status = 'success' "
                "LIMIT 1",
                (task_id,),
            ).fetchone()
        return row is not None
