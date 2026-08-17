"""工具执行总入口：被 AgentLoop 调用，串联 Registry、Safety、Approval 和 AuditRepository。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..errors import ApprovalDeniedError, AuditError, NexusError
from ..orchestrator.schemas import ToolCall, ToolResult
from ..safety.approval import ApprovalHandler
from ..safety.redaction import action_fingerprint, content_sha256, redact_text, truncate_text
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

        audit_arguments = self._audit_arguments(tool_call.name, tool_call.arguments)
        audit_id = self._start_tool_call(task_id, tool_call, audit_arguments)
        tool = self.registry.get(tool_call.name)
        if tool is None:
            message = f"工具调用失败：未知工具 {tool_call.name!r}，当前只允许 read、write、edit、bash"
            try:
                self._finish_tool_call(
                    audit_id,
                    status="failed",
                    result_summary=message,
                    error_type="UnknownTool",
                    error_message=message,
                )
            except AuditError as exc:
                self._best_effort_finish(
                    audit_id,
                    status="failed",
                    result_summary="未知工具审计失败：任务已终止",
                    error_type=type(exc).__name__,
                    error_message="未知工具审计失败：任务已终止",
                )
                raise
            return ToolResult(tool_call.id, message, is_error=True, metadata={"error_type": "UnknownTool"})

        try:
            arguments = tool.validate(tool_call.arguments)
            tool.check_safety(arguments)
            approval = tool.approval_request(arguments)
            if approval is not None:
                approved = await self.approvals.request(approval)
                if not approved:
                    self._record_approval(task_id, tool_call, approval, False)
                    raise ApprovalDeniedError(
                        f"工具 {tool.name} 执行被拒绝：用户未批准 {approval.action_type} 操作"
                    )
                refreshed = tool.approval_request(arguments)
                if refreshed is None or refreshed.fingerprint != approval.fingerprint:
                    self._record_approval(task_id, tool_call, approval, False)
                    raise ApprovalDeniedError(
                        f"工具 {tool.name} 执行被拒绝：审批后动作摘要发生变化，必须重新审批"
                    )
                self._record_approval(task_id, tool_call, refreshed, True)
                arguments["_approved_fingerprint"] = refreshed.fingerprint
                latest = tool.approval_request(arguments)
                if latest is None or latest.fingerprint != refreshed.fingerprint:
                    self._record_approval(task_id, tool_call, refreshed, False)
                    raise ApprovalDeniedError(
                        f"工具 {tool.name} 执行被拒绝：执行前目标状态发生变化，必须重新审批"
                    )
            output = await tool.execute(arguments, cancel_event)
            result = ToolResult(
                tool_call.id,
                self._model_content(
                    output.content,
                    output.metadata,
                    self.registry.model_message_budget(),
                ),
                is_error=output.is_error,
                metadata=output.metadata,
            )
            summary = self._audit_summary(tool.name, output.metadata, output.is_error)
            self._finish_tool_call(
                audit_id,
                status="failed" if output.is_error else "success",
                result_summary=summary,
                error_type="ToolExecutionError" if output.is_error else None,
                error_message=summary if output.is_error else None,
            )
            return result
        except asyncio.CancelledError:
            self._finish_tool_call(
                audit_id,
                status="cancelled",
                result_summary="工具调用已取消：任务收到取消信号",
                error_type="CancelledError",
                error_message="工具调用已取消：任务收到取消信号",
            )
            raise
        except AuditError as exc:
            self._best_effort_finish(
                audit_id,
                status="failed",
                result_summary=redact_text(f"工具审计失败：{type(exc).__name__}"),
                error_type=type(exc).__name__,
                error_message="工具审计失败：任务已终止",
            )
            raise
        # 工具边界统一记录未知异常，保留工具名和调用 ID 以便审计定位。
        except Exception as exc:  # noqa: BLE001
            message = self._error_message(tool.name, exc)
            status = "rejected" if isinstance(exc, ApprovalDeniedError) else "failed"
            self._finish_tool_call(
                audit_id,
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

    def _start_tool_call(self, task_id: str, tool_call: ToolCall, arguments: str) -> str:
        """启动审计失败时立即抛出系统错误，交给 Agent Loop 终结任务。"""

        try:
            return self.audit_repository.start_tool_call(
                task_id, tool_call.id, tool_call.name, arguments
            )
        except Exception as exc:
            raise AuditError(
                f"工具审计启动失败：工具 {tool_call.name}，调用 {tool_call.id}，"
                f"原因：{type(exc).__name__}"
            ) from exc

    def _finish_tool_call(self, audit_id: str, **kwargs: Any) -> None:
        """审计结束失败不能被转换成普通工具错误，避免任务假装成功。"""

        try:
            self.audit_repository.finish_tool_call(audit_id, **kwargs)
        except Exception as exc:
            raise AuditError(
                f"工具审计结束失败：审计记录 {audit_id}，原因：{type(exc).__name__}"
            ) from exc

    def _best_effort_finish(self, audit_id: str, **kwargs: Any) -> None:
        """审计异常后尝试补写终态，补写失败仍由原始 AuditError 交给编排层。"""

        try:
            self.audit_repository.finish_tool_call(audit_id, **kwargs)
        except Exception:  # noqa: BLE001
            return

    def _record_approval(
        self, task_id: str, tool_call: ToolCall, approval, decision: bool
    ) -> None:
        """只保存审批动作的安全摘要，不把审批预览正文写入数据库。"""

        try:
            audit_summary = approval.audit_summary or (
                f"动作类型={approval.action_type}，审批指纹={approval.fingerprint}"
            )
            self.audit_repository.record_approval(
                task_id,
                tool_call.id,
                approval.action_type,
                redact_text(audit_summary),
                decision,
            )
        except Exception as exc:
            raise AuditError(
                f"审批审计写入失败：工具 {tool_call.name}，调用 {tool_call.id}，"
                f"原因：{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _audit_arguments(tool_name: str, arguments: object) -> str:
        """按工具投影审计参数，只保存路径、长度、动作和摘要哈希。"""

        values = arguments if isinstance(arguments, dict) else {}
        if tool_name == "read":
            projection = {
                "path": values.get("path"),
                "offset": values.get("offset", 0),
                "byte_offset": values.get("byte_offset", 0),
                "limit": values.get("limit"),
            }
        elif tool_name == "write":
            content = values.get("content")
            projection = {
                "path": values.get("path"),
                "content_bytes": len(content.encode("utf-8")) if isinstance(content, str) else None,
                "content_sha256": content_sha256(content) if isinstance(content, str) else None,
            }
        elif tool_name == "edit":
            old_text = values.get("old_text")
            new_text = values.get("new_text")
            projection = {
                "path": values.get("path"),
                "old_text_bytes": len(old_text.encode("utf-8")) if isinstance(old_text, str) else None,
                "new_text_bytes": len(new_text.encode("utf-8")) if isinstance(new_text, str) else None,
                "old_text_sha256": content_sha256(old_text) if isinstance(old_text, str) else None,
                "new_text_sha256": content_sha256(new_text) if isinstance(new_text, str) else None,
            }
        elif tool_name == "bash":
            command = values.get("command")
            projection = {
                "command_sha256": content_sha256(command) if isinstance(command, str) else None,
                "timeout_seconds": values.get("timeout_seconds"),
            }
        else:
            projection = {
                "keys": sorted(str(key) for key in values),
                "value_types": {str(key): type(value).__name__ for key, value in values.items()},
            }
        return str({"tool": tool_name, "fingerprint": action_fingerprint(projection), **projection})

    @staticmethod
    def _model_content(
        content: str, metadata: dict[str, Any], budget: int | None = None
    ) -> str:
        """在统一字节预算内保留正文和安全状态，避免截断信息被二次截掉。"""

        allowed_keys = {
            "path",
            "offset",
            "byte_offset",
            "next_offset",
            "next_byte_offset",
            "next_cursor",
            "bytes",
            "truncated",
            "exit_code",
            "timed_out",
            "cancelled",
            "action",
            "matches",
            "diff_truncated",
        }
        safe_metadata = {
            key: value for key, value in metadata.items() if key in allowed_keys
        }
        if not safe_metadata:
            return truncate_text(content, budget)[0] if budget is not None else content
        status = ToolExecutor._status_envelope(safe_metadata, budget)
        if budget is None:
            return content + status
        body_budget = max(0, budget - len(status.encode("utf-8")))
        body = ToolExecutor._bounded_model_body(
            content, body_budget, bool(safe_metadata.get("truncated"))
        )
        return body + status

    @staticmethod
    def _status_envelope(metadata: dict[str, Any], budget: int | None) -> str:
        """按优先级压缩状态字段，确保小预算仍能传递游标或截断状态。"""

        serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        status = f"\n[工具状态] {serialized}"
        if budget is None or len(status.encode("utf-8")) <= budget:
            return status
        priority = ("next_cursor", "truncated", "exit_code", "timed_out", "cancelled")
        compact = {key: metadata[key] for key in priority if key in metadata}
        serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        status = f"\n[状态] {serialized}"
        if len(status.encode("utf-8")) <= budget:
            return status
        return "!" if metadata.get("truncated") else ""

    @staticmethod
    def _bounded_model_body(content: str, budget: int, truncated: bool) -> str:
        """截取模型正文并尽量保留中文截断标记，状态信封负责最终可见性。"""

        if not truncated:
            return truncate_text(content, budget)[0]
        marker = "\n[输出已截断]"
        if len(marker.encode("utf-8")) > budget:
            return truncate_text(content, budget)[0]
        source = content.removesuffix(marker)
        prefix, _ = truncate_text(source, budget - len(marker.encode("utf-8")))
        return prefix + marker

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
