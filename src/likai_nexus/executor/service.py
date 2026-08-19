"""工具执行总入口：被 AgentLoop 调用，串联 Registry、Safety、Approval 和 AuditRepository。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..errors import ApprovalDeniedError, AuditError, ConfigError, NexusError
from ..events import EventSink, NullEventSink, RuntimeEvent, emit_safely
from ..safety.approval import ApprovalHandler, ApprovalRequest
from ..safety.redaction import (
    redact_text,
    redact_value,
    safe_audit_identifier,
    sanitize_terminal_text,
    sanitize_terminal_value,
)
from ..safety.review_mode import ReviewMode, parse_review_mode
from ..storage.audit_repository import AuditRepository
from ..tools.base import Tool, ToolOutput, safe_argument_summary
from ..tools.contracts import (
    ToolAuditProjection,
    ToolCall,
    ToolDisplayProjection,
    ToolModelProjection,
    ToolResult,
    ToolStatus,
)
from ..tools.registry import ToolRegistry
from .audit import AuditLifecycle
from .projection import ToolProjectionService


class ToolExecutor:
    """唯一允许 Agent Loop 调用具体工具的服务。"""

    _DISPLAY_COMMAND_BYTES = 4 * 1024
    _DISPLAY_RESULT_BYTES = 1 * 1024

    def __init__(
        self,
        registry: ToolRegistry,
        approvals: ApprovalHandler,
        audit_repository: AuditRepository,
        review_mode: ReviewMode | str | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.registry = registry
        self.approvals = approvals
        self.audit_repository = audit_repository
        registry_mode = registry.review_mode
        requested_mode = (
            registry_mode if review_mode is None else parse_review_mode(review_mode)
        )
        if requested_mode is not registry_mode:
            raise ConfigError(
                "执行器配置失败：ToolExecutor 的审查模式 "
                f"{requested_mode.value} 与 ToolRegistry 的审查模式 {registry_mode.value} 不一致"
            )
        self.review_mode = registry_mode
        self.event_sink = event_sink or NullEventSink()
        self.projection = ToolProjectionService(
            registry.model_message_budget(),
            self._DISPLAY_COMMAND_BYTES,
            self._DISPLAY_RESULT_BYTES,
        )
        self.audit = AuditLifecycle(audit_repository)

    async def execute(
        self,
        task_id: str,
        tool_call: ToolCall,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolResult:
        """执行一次工具调用，所有可预期失败均回填标准错误结果。"""

        tool = self.registry.get(tool_call.name)
        audit_arguments = self._audit_arguments(tool, tool_call.arguments)
        tool_label = self.safe_tool_label(tool_call.name)
        audit_id = self._start_tool_call(task_id, tool_call, audit_arguments, tool_label)
        started_at = time.monotonic()
        display_invocation = self.projection.display_arguments(tool, tool_call.arguments)
        self._emit(
            "tool_started",
            task_id,
            f"工具开始：{tool_label}",
            {
                "tool_name": tool_label,
                "status": "started",
                "invocation": display_invocation,
            },
        )
        if tool is None:
            names = ", ".join(spec.name for spec in self.registry.specs()) or "无"
            message = (
                f"工具调用失败：未知工具 {tool_label!r}，当前可用工具：{names}，"
                f"耗时={self._elapsed_ms(started_at)}ms"
            )
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
            self._emit(
                "tool_failed",
                task_id,
                message,
                {
                    "tool_name": tool_label,
                    "status": "failed",
                    "elapsed_ms": self._elapsed_ms(started_at),
                    "reason": message,
                },
            )
            return ToolResult(
                tool_call.id,
                ToolStatus.FAILED,
                ToolModelProjection(
                    self.projection.model_content(
                        message,
                        {"error_type": "UnknownTool"},
                        self.registry.model_message_budget(),
                    ),
                    {"error_type": "UnknownTool"},
                ),
                audit=ToolAuditProjection(audit_arguments, message),
            )

        try:
            arguments = tool.validate(tool_call.arguments)
            self._emit("safety_started", task_id, f"安全检查开始：工具 {tool.name}")
            tool.check_safety(arguments)
            self._emit("safety_passed", task_id, f"安全检查通过：工具 {tool.name}")
            approval = tool.approval_request(arguments)
            if approval is not None:
                self._emit(
                    "approval_requested",
                    task_id,
                    f"等待人工审批：工具 {tool.name}，动作={approval.action_type}",
                )
                approved = await self.approvals.request(approval)
                if not approved:
                    self._record_approval(task_id, tool_call, approval, False)
                    self._emit(
                        "approval_denied",
                        task_id,
                        f"人工审批拒绝：工具 {tool.name}，动作={approval.action_type}",
                    )
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
                self._emit(
                    "approval_approved",
                    task_id,
                    f"人工审批通过：工具 {tool.name}，动作={refreshed.action_type}",
                )
                arguments["_approved_fingerprint"] = refreshed.fingerprint
                latest = tool.approval_request(arguments)
                if latest is None or latest.fingerprint != refreshed.fingerprint:
                    self._record_approval(task_id, tool_call, refreshed, False)
                    raise ApprovalDeniedError(
                        f"工具 {tool.name} 执行被拒绝：执行前目标状态发生变化，必须重新审批"
                    )
            else:
                automatic = self._automatic_approval(tool, arguments)
                self._record_approval(task_id, tool_call, automatic, True, "mode")
                self._emit(
                    "approval_auto_allowed",
                    task_id,
                    f"模式自动允许：工具 {tool.name}，模式={self.review_mode.value}",
                )
            output = await tool.execute(arguments, cancel_event)
            summary = self.projection.audit_summary(tool, output)
            status = self._tool_status(output)
            display = self.projection.display_result(tool, output)
            result = self._tool_result(
                tool, tool_call.id, output, audit_arguments, summary, display
            )
            self._finish_tool_call(
                audit_id,
                status=status.value,
                result_summary=summary,
                error_type="ToolExecutionError" if status.is_error else None,
                error_message=summary if status.is_error else None,
            )
            event_metadata = self._tool_event_metadata(
                tool,
                status.value,
                self._elapsed_ms(started_at),
                summary,
                display,
            )
            self._emit(
                self._tool_event_type(status.value),
                task_id,
                f"工具{self._status_label(status.value)}：{tool.name}，"
                f"耗时={self._elapsed_ms(started_at)}ms，{summary}",
                event_metadata,
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
            self._emit(
                "tool_cancelled",
                task_id,
                f"工具取消：{tool.name}，耗时={self._elapsed_ms(started_at)}ms，"
                "任务收到取消信号",
                self._tool_event_metadata(
                    tool,
                    "cancelled",
                    self._elapsed_ms(started_at),
                    "任务收到取消信号",
                    ToolDisplayProjection(),
                ),
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
            self._emit(
                "tool_failed",
                task_id,
                f"工具审计失败：{tool.name}，耗时={self._elapsed_ms(started_at)}ms，"
                f"原因={type(exc).__name__}",
                self._tool_event_metadata(
                    tool,
                    "failed",
                    self._elapsed_ms(started_at),
                    f"审计失败：{type(exc).__name__}",
                    ToolDisplayProjection(),
                ),
            )
            raise
        # 工具边界统一记录未知异常，保留工具名和调用 ID 以便审计定位。
        except Exception as exc:  # noqa: BLE001
            message = self._error_message(tool.name, exc)
            tool_status = (
                ToolStatus.REJECTED if isinstance(exc, ApprovalDeniedError) else ToolStatus.FAILED
            )
            self._finish_tool_call(
                audit_id,
                status=tool_status.value,
                result_summary=message,
                error_type=type(exc).__name__,
                error_message=message,
            )
            rejected = isinstance(exc, ApprovalDeniedError)
            self._emit(
                "tool_rejected" if rejected else "tool_failed",
                task_id,
                f"工具{'拒绝' if rejected else '失败'}：{tool.name}，"
                f"耗时={self._elapsed_ms(started_at)}ms，{message}",
                self._tool_event_metadata(
                    tool,
                    tool_status.value,
                    self._elapsed_ms(started_at),
                    message,
                    ToolDisplayProjection(),
                ),
            )
            return ToolResult(
                tool_call.id,
                tool_status,
                ToolModelProjection(
                    self.projection.model_content(
                        message,
                        {"error_type": type(exc).__name__},
                        self.registry.model_message_budget(),
                    ),
                    {"error_type": type(exc).__name__},
                ),
                audit=ToolAuditProjection(audit_arguments, message),
            )

    def _tool_result(
        self,
        tool: Tool,
        tool_call_id: str,
        output: ToolOutput,
        audit_arguments: str,
        audit_summary: str,
        display: ToolDisplayProjection,
    ) -> ToolResult:
        """统一生成回填模型的工具结果，所有成功和错误分支共享总预算。"""

        metadata = self.projection.model_metadata(tool, output)
        return ToolResult(
            tool_call_id,
            output.effective_status(),
            ToolModelProjection(
                self.projection.model_content(
                    output.content,
                    metadata,
                    self.registry.model_message_budget(),
                    self.projection.model_metadata_priority(tool, output, metadata),
                ),
                metadata,
            ),
            display=display,
            audit=ToolAuditProjection(audit_arguments, audit_summary),
        )

    def _start_tool_call(
        self, task_id: str, tool_call: ToolCall, arguments: str, tool_label: str
    ) -> str:
        """保留执行器门面方法，实际生命周期由 AuditLifecycle 负责。"""

        return self.audit.start_tool_call(task_id, tool_call, arguments, tool_label)

    def _finish_tool_call(self, audit_id: str, **kwargs: Any) -> None:
        """保留执行器门面方法，实际终态记录由 AuditLifecycle 负责。"""

        self.audit.finish_tool_call(audit_id, **kwargs)

    def _best_effort_finish(self, audit_id: str, **kwargs: Any) -> None:
        """保留执行器门面方法，实际补写由 AuditLifecycle 负责。"""

        self.audit.best_effort_finish(audit_id, **kwargs)

    def _record_approval(
        self,
        task_id: str,
        tool_call: ToolCall,
        approval,
        decision: bool,
        decision_source: str = "human",
    ) -> None:
        """保留执行器门面方法，实际审批记录由 AuditLifecycle 负责。"""

        self.audit.record_approval(
            task_id,
            tool_call,
            approval,
            decision,
            decision_source,
            self.safe_tool_label(tool_call.name),
        )

    def _audit_arguments(self, tool: Tool | None, arguments: object) -> str:
        """委托工具生成参数摘要，未声明时退回不含值的保守摘要。"""

        if tool is None:
            return redact_text(safe_argument_summary("unknown", arguments))
        try:
            return redact_text(tool.audit_arguments(arguments))
        except Exception:  # noqa: BLE001
            return redact_text(safe_argument_summary(tool.name, arguments))

    @staticmethod
    def _model_content(
        content: str,
        metadata: dict[str, Any],
        budget: int | None = None,
        metadata_priority: tuple[str, ...] | None = None,
    ) -> str:
        """兼容旧测试和扩展调用方，具体模型投影由独立服务实现。"""

        return ToolProjectionService(
            budget or 0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).model_content(content, metadata, budget, metadata_priority)

    @staticmethod
    def _status_envelope(
        metadata: dict[str, Any],
        budget: int | None,
        metadata_priority: tuple[str, ...] | None = None,
    ) -> str:
        """兼容旧调用方，具体状态信封由独立投影服务实现。"""

        return ToolProjectionService(
            budget or 0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).status_envelope(metadata, budget, metadata_priority)

    @staticmethod
    def _bounded_model_body(content: str, budget: int, truncated: bool) -> str:
        """兼容旧调用方，具体正文截断由独立投影服务实现。"""

        return ToolProjectionService(
            budget,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).bounded_model_body(content, budget, truncated)

    def _automatic_approval(
        self, tool: Tool, arguments: dict[str, Any]
    ) -> ApprovalRequest:
        """为模式自动允许的动作生成安全审批记录，不暴露原始参数。"""

        return ApprovalRequest(
            action_type=f"{tool.name}_auto_allowed",
            summary=f"审查模式 {self.review_mode.value} 自动允许工具 {tool.name}",
            audit_summary=(
                f"模式={self.review_mode.value}，工具={tool.name}，"
                f"参数={self._audit_arguments(tool, arguments)}"
            ),
        )

    @staticmethod
    def _model_metadata(tool: Tool, output: ToolOutput) -> dict[str, Any]:
        """兼容旧调用方，具体模型状态由独立投影服务实现。"""

        return ToolProjectionService(0, ToolExecutor._DISPLAY_COMMAND_BYTES, ToolExecutor._DISPLAY_RESULT_BYTES).model_metadata(tool, output)

    @staticmethod
    def _model_metadata_priority(
        tool: Tool, output: ToolOutput, metadata: dict[str, Any]
    ) -> tuple[str, ...]:
        """兼容旧调用方，具体状态优先级由独立投影服务实现。"""

        return ToolProjectionService(0, ToolExecutor._DISPLAY_COMMAND_BYTES, ToolExecutor._DISPLAY_RESULT_BYTES).model_metadata_priority(tool, output, metadata)

    @staticmethod
    def _audit_summary(tool: Tool, output: ToolOutput) -> str:
        """兼容旧调用方，具体审计摘要由独立投影服务实现。"""

        return ToolProjectionService(0, ToolExecutor._DISPLAY_COMMAND_BYTES, ToolExecutor._DISPLAY_RESULT_BYTES).audit_summary(tool, output)

    @staticmethod
    def _display_arguments(tool: Tool | None, arguments: object) -> str:
        """兼容旧调用方，具体参数展示由独立投影服务实现。"""

        return ToolProjectionService(
            0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).display_arguments(tool, arguments)

    @staticmethod
    def _display_result(
        tool: Tool | None, output: ToolOutput | None
    ) -> ToolDisplayProjection:
        """兼容旧调用方，具体结果展示由独立投影服务实现。"""

        return ToolProjectionService(
            0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).display_result(tool, output)

    @classmethod
    def _tool_event_metadata(
        cls,
        tool: Tool | None,
        status: str,
        elapsed_ms: int,
        reason: str | None,
        display: ToolDisplayProjection,
    ) -> dict[str, Any]:
        return ToolProjectionService(
            0,
            cls._DISPLAY_COMMAND_BYTES,
            cls._DISPLAY_RESULT_BYTES,
        ).event_metadata(tool, status, elapsed_ms, reason, display)

    @staticmethod
    def _tool_status(output: ToolOutput) -> ToolStatus:
        return output.effective_status()

    @staticmethod
    def _tool_event_type(status: str) -> str:
        return {
            "success": "tool_finished",
            "timeout": "tool_timed_out",
            "cancelled": "tool_cancelled",
            "rejected": "tool_rejected",
        }.get(status, "tool_failed")

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "success": "成功",
            "failed": "失败",
            "timeout": "超时",
            "cancelled": "取消",
            "rejected": "拒绝",
        }.get(status, status)

    @staticmethod
    def _short_reason(reason: str) -> str:
        return ToolProjectionService(
            0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).short_reason(reason)

    @staticmethod
    def _bounded_display_text(value: str, limit: int, marker: str) -> str:
        return ToolProjectionService(
            0,
            ToolExecutor._DISPLAY_COMMAND_BYTES,
            ToolExecutor._DISPLAY_RESULT_BYTES,
        ).bounded_display_text(value, limit, marker)

    @staticmethod
    def _tool_label(tool_name: str, tool: Tool | None) -> str:
        """为过程和未知工具错误生成不携带原始敏感标识的工具标签。"""

        if tool is not None:
            return tool.name
        return safe_audit_identifier(tool_name, "unknown-tool")

    def safe_tool_label(self, tool_name: str) -> str:
        """返回 Registry 确认的工具名，未知名称只返回安全标签。"""

        return self._tool_label(tool_name, self.registry.get(tool_name))

    def record_mode_confirmation(self, task_id: str, decision_source: str = "human") -> None:
        """记录 full-access 的首次人工确认或本地偏好沿用。"""

        self.audit.record_mode_confirmation(
            task_id, self.review_mode.value, decision_source
        )

    def _emit(
        self,
        event_type: str,
        task_id: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_metadata = (
            redact_value(sanitize_terminal_value(metadata)) if metadata is not None else {}
        )
        emit_safely(
            self.event_sink,
            RuntimeEvent(
                event_type,
                task_id,
                redact_text(sanitize_terminal_text(message)),
                safe_metadata,
            ),
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((time.monotonic() - started_at) * 1000))

    @staticmethod
    def _error_message(tool_name: str, error: Exception) -> str:
        if isinstance(error, NexusError):
            detail = str(error)
        else:
            detail = f"{type(error).__name__}: {error}"
        return redact_text(f"工具 {tool_name} 执行失败：{detail}")
