"""工具审计生命周期：集中处理启动、终态、审批和模式确认记录。"""

from __future__ import annotations

from typing import Any

from ..errors import AuditError
from ..safety.approval import ApprovalRequest
from ..safety.redaction import redact_text, safe_audit_identifier
from ..storage.audit_repository import AuditRepository
from ..tools.contracts import ToolCall


class AuditLifecycle:
    """把审计存储异常转换为可定位的 AuditError，避免执行器重复样板代码。"""

    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository

    def start_tool_call(
        self, task_id: str, tool_call: ToolCall, arguments: str, tool_label: str
    ) -> str:
        """启动工具审计；启动失败必须阻止实际工具执行。"""

        try:
            return self.repository.start_tool_call(
                task_id,
                tool_call.id,
                tool_label,
                arguments,
                canonical_tool_name=True,
            )
        except Exception as exc:
            raise AuditError(
                f"工具审计启动失败：工具 {tool_label}，调用 {safe_audit_identifier(tool_call.id, 'call')}，"
                f"原因：{type(exc).__name__}"
            ) from exc

    def finish_tool_call(self, audit_id: str, **kwargs: Any) -> None:
        """写入工具终态；失败时保持系统错误语义。"""

        try:
            self.repository.finish_tool_call(audit_id, **kwargs)
        except Exception as exc:
            raise AuditError(
                f"工具审计结束失败：审计记录 {audit_id}，原因：{type(exc).__name__}"
            ) from exc

    def best_effort_finish(self, audit_id: str, **kwargs: Any) -> None:
        """审计异常后尽力补写终态，不能覆盖原始异常。"""

        try:
            self.repository.finish_tool_call(audit_id, **kwargs)
        except Exception:  # noqa: BLE001
            return

    def record_approval(
        self,
        task_id: str,
        tool_call: ToolCall,
        approval: ApprovalRequest,
        decision: bool,
        decision_source: str,
        tool_label: str,
    ) -> None:
        """保存人工或模式决定的安全摘要，不把审批预览正文写入数据库。"""

        try:
            audit_summary = approval.audit_summary or (
                f"动作类型={approval.action_type}，审批指纹={approval.fingerprint}"
            )
            self.repository.record_approval(
                task_id,
                tool_call.id,
                approval.action_type,
                redact_text(audit_summary),
                decision,
                decision_source,
            )
        except Exception as exc:
            raise AuditError(
                f"审批审计写入失败：工具 {tool_label}，"
                f"调用 {safe_audit_identifier(tool_call.id, 'call')}，"
                f"原因：{type(exc).__name__}"
            ) from exc

    def record_mode_confirmation(
        self, task_id: str, mode: str, decision_source: str = "human"
    ) -> None:
        """记录 full-access 的首次人工确认或本地偏好沿用。"""

        request = ApprovalRequest(
            action_type="full_access_session",
            summary="任务级完全访问确认",
            audit_summary=f"任务审查模式={mode}，启动确认来源={decision_source}",
        )
        try:
            self.repository.record_approval(
                task_id,
                "__task__",
                request.action_type,
                request.audit_summary,
                True,
                decision_source,
            )
        except Exception as exc:
            raise AuditError(
                f"完全访问确认审计写入失败：任务 {task_id}，原因：{type(exc).__name__}"
            ) from exc
