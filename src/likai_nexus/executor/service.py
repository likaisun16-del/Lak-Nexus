"""工具执行总入口：按查找、校验、安全、审批、执行、审计顺序处理每次调用。"""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import ApprovalDeniedError, NexusError
from ..orchestrator.schemas import ToolCall, ToolResult
from ..safety.approval import ApprovalHandler
from ..safety.redaction import redact_arguments, redact_text
from ..storage.audit_repository import AuditRepository
from .registry import ToolRegistry


class ToolExecutor:
    """唯一允许 Agent Loop 调用具体工具的服务。"""

    def __init__(
        self, registry: ToolRegistry, approvals: ApprovalHandler, audit_repository: AuditRepository
    ) -> None:
        self.registry = registry
        self.approvals = approvals
        self.audit_repository = audit_repository

    async def execute(
        self,
        task_id: str,
        tool_call: ToolCall,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolResult:
        """执行一次工具调用，所有可预期失败均回填标准错误结果。"""

        redacted_arguments = redact_arguments(tool_call.arguments)
        self.audit_repository.start_tool_call(
            task_id, tool_call.id, tool_call.name, redacted_arguments
        )
        tool = self.registry.get(tool_call.name)
        if tool is None:
            message = f"工具调用失败：未知工具 {tool_call.name!r}，当前只允许 read、write、edit、bash"
            self.audit_repository.finish_tool_call(
                tool_call.id,
                status="failed",
                result_summary=message,
                error_type="UnknownTool",
                error_message=message,
            )
            return ToolResult(tool_call.id, message, is_error=True, metadata={"error_type": "UnknownTool"})

        try:
            arguments = tool.validate(tool_call.arguments)
            tool.check_safety(arguments)
            approval = tool.approval_request(arguments)
            if approval is not None:
                approved = await self.approvals.request(approval)
                self.audit_repository.record_approval(
                    task_id,
                    tool_call.id,
                    approval.action_type,
                    redact_text(approval.summary),
                    approved,
                )
                if not approved:
                    raise ApprovalDeniedError(
                        f"工具 {tool.name} 执行被拒绝：用户未批准 {approval.action_type} 操作"
                    )
            output = await tool.execute(arguments, cancel_event)
            result = ToolResult(
                tool_call.id,
                output.content,
                is_error=output.is_error,
                metadata=output.metadata,
            )
            summary = self._audit_summary(tool.name, output.metadata, output.is_error)
            self.audit_repository.finish_tool_call(
                tool_call.id,
                status="failed" if output.is_error else "success",
                result_summary=summary,
                error_type="ToolExecutionError" if output.is_error else None,
                error_message=summary if output.is_error else None,
            )
            return result
        except asyncio.CancelledError:
            self.audit_repository.finish_tool_call(
                tool_call.id,
                status="cancelled",
                result_summary="工具调用已取消：任务收到取消信号",
                error_type="CancelledError",
                error_message="工具调用已取消：任务收到取消信号",
            )
            raise
        # 工具边界统一记录未知异常，保留工具名和调用 ID 以便审计定位。
        except Exception as exc:  # noqa: BLE001
            message = self._error_message(tool.name, exc)
            status = "rejected" if isinstance(exc, ApprovalDeniedError) else "failed"
            self.audit_repository.finish_tool_call(
                tool_call.id,
                status=status,
                result_summary=message,
                error_type=type(exc).__name__,
                error_message=message,
            )
            return ToolResult(
                tool_call.id,
                message,
                is_error=True,
                metadata={"error_type": type(exc).__name__},
            )

    @staticmethod
    def _audit_summary(tool_name: str, metadata: dict[str, Any], is_error: bool) -> str:
        """只保存结构化摘要，避免把文件正文、diff 或 Bash 输出写入审计库。"""

        state = "失败" if is_error else "成功"
        if tool_name == "read":
            return (
                f"read {state}：路径={metadata.get('path', '[未知]')}，"
                f"字节数={metadata.get('bytes', 0)}，截断={metadata.get('truncated', False)}"
            )
        if tool_name == "write":
            return (
                f"write {state}：路径={metadata.get('path', '[未知]')}，"
                f"动作={metadata.get('action', '[未知]')}，字节数={metadata.get('bytes', 0)}"
            )
        if tool_name == "edit":
            return (
                f"edit {state}：路径={metadata.get('path', '[未知]')}，"
                f"匹配数={metadata.get('matches', 0)}，diff截断={metadata.get('diff_truncated', False)}"
            )
        if tool_name == "bash":
            return (
                f"bash {state}：退出码={metadata.get('exit_code')}，"
                f"超时={metadata.get('timed_out', False)}，取消={metadata.get('cancelled', False)}，"
                f"输出截断={metadata.get('truncated', False)}"
            )
        return f"工具 {tool_name} {state}"

    @staticmethod
    def _error_message(tool_name: str, error: Exception) -> str:
        if isinstance(error, NexusError):
            detail = str(error)
        else:
            detail = f"{type(error).__name__}: {error}"
        return redact_text(f"工具 {tool_name} 执行失败：{detail}")
