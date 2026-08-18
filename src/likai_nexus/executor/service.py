"""工具执行总入口：被 AgentLoop 调用，串联 Registry、Safety、Approval 和 AuditRepository。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..errors import ApprovalDeniedError, AuditError, ConfigError, NexusError
from ..orchestrator.events import EventSink, NullEventSink, RuntimeEvent, emit_safely
from ..orchestrator.schemas import ToolCall, ToolResult
from ..safety.approval import ApprovalHandler, ApprovalRequest
from ..safety.redaction import redact_text, redact_value, safe_audit_identifier, truncate_text
from ..safety.review_mode import ReviewMode, parse_review_mode
from ..storage.audit_repository import AuditRepository
from .base import Tool, ToolOutput, safe_argument_summary
from .registry import ToolRegistry


class ToolExecutor:
    """唯一允许 Agent Loop 调用具体工具的服务。"""

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
        self._emit(
            "tool_started",
            task_id,
            f"工具开始：{tool_label}，{self._display_arguments(tool, tool_call.arguments)}",
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
            self._emit("tool_failed", task_id, message)
            return ToolResult(
                tool_call.id,
                self._model_content(
                    message,
                    {"error_type": "UnknownTool"},
                    self.registry.model_message_budget(),
                ),
                is_error=True,
                metadata={"error_type": "UnknownTool"},
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
            summary = self._audit_summary(tool, output)
            result = self._tool_result(tool, tool_call.id, output)
            self._finish_tool_call(
                audit_id,
                status="failed" if output.is_error else "success",
                result_summary=summary,
                error_type="ToolExecutionError" if output.is_error else None,
                error_message=summary if output.is_error else None,
            )
            status = "failed" if output.is_error else "success"
            self._emit(
                "tool_failed" if output.is_error else "tool_finished",
                task_id,
                f"工具{status}：{tool.name}，耗时={self._elapsed_ms(started_at)}ms，{summary}",
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
            rejected = isinstance(exc, ApprovalDeniedError)
            self._emit(
                "tool_rejected" if rejected else "tool_failed",
                task_id,
                f"工具{'拒绝' if rejected else '失败'}：{tool.name}，"
                f"耗时={self._elapsed_ms(started_at)}ms，{message}",
            )
            return ToolResult(
                tool_call.id,
                self._model_content(
                    message,
                    {"error_type": type(exc).__name__},
                    self.registry.model_message_budget(),
                ),
                is_error=True,
                metadata={"error_type": type(exc).__name__},
            )

    def _tool_result(self, tool: Tool, tool_call_id: str, output: ToolOutput) -> ToolResult:
        """统一生成回填模型的工具结果，所有成功和错误分支共享总预算。"""

        metadata = self._model_metadata(tool, output)
        return ToolResult(
            tool_call_id,
            self._model_content(
                output.content,
                metadata,
                self.registry.model_message_budget(),
                self._model_metadata_priority(tool, output, metadata),
            ),
            is_error=output.is_error,
            metadata=output.metadata,
        )

    def _start_tool_call(
        self, task_id: str, tool_call: ToolCall, arguments: str, tool_label: str
    ) -> str:
        """启动审计失败时立即抛出系统错误，交给 Agent Loop 终结任务。"""

        try:
            return self.audit_repository.start_tool_call(
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
        self,
        task_id: str,
        tool_call: ToolCall,
        approval,
        decision: bool,
        decision_source: str = "human",
    ) -> None:
        """保存人工或模式决定的安全摘要，不把审批预览正文写入数据库。"""

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
                decision_source,
            )
        except Exception as exc:
            raise AuditError(
                f"审批审计写入失败：工具 {self.safe_tool_label(tool_call.name)}，"
                f"调用 {safe_audit_identifier(tool_call.id, 'call')}，"
                f"原因：{type(exc).__name__}"
            ) from exc

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
        """在统一字节预算内保留正文和安全状态，避免截断信息被二次截掉。"""

        content = redact_text(content)
        safe_metadata = dict(metadata)
        if not safe_metadata:
            return truncate_text(content, budget)[0] if budget is not None else content
        status = ToolExecutor._status_envelope(safe_metadata, budget, metadata_priority)
        if budget is None:
            return content + status
        if len((content + status).encode("utf-8")) <= budget:
            return content + status
        body_budget = max(0, budget - len(status.encode("utf-8")))
        if len(content.encode("utf-8")) > body_budget and not safe_metadata.get("truncated"):
            safe_metadata = {**safe_metadata, "truncated": True}
            status = ToolExecutor._status_envelope(safe_metadata, budget, metadata_priority)
            body_budget = max(0, budget - len(status.encode("utf-8")))
        body = ToolExecutor._bounded_model_body(
            content,
            body_budget,
            bool(safe_metadata.get("truncated")),
        )
        return body + status

    @staticmethod
    def _status_envelope(
        metadata: dict[str, Any],
        budget: int | None,
        metadata_priority: tuple[str, ...] | None = None,
    ) -> str:
        """按工具声明的字段顺序压缩状态，确保小预算仍能传递必要状态。"""

        serialized = redact_text(
            json.dumps(redact_value(metadata), ensure_ascii=False, sort_keys=True)
        )
        status = f"\n[工具状态] {serialized}"
        if budget is None or len(status.encode("utf-8")) <= budget:
            return status
        priority = tuple(metadata_priority or ())
        ordered_keys = priority + tuple(key for key in metadata if key not in priority)
        compact: dict[str, Any] = {}
        for key in ordered_keys:
            candidate = {**compact, key: metadata[key]}
            serialized = redact_text(
                json.dumps(redact_value(candidate), ensure_ascii=False, separators=(",", ":"))
            )
            status = f"\n[状态] {serialized}"
            if len(status.encode("utf-8")) <= budget:
                compact = candidate
            elif compact:
                break
        if compact:
            serialized = redact_text(
                json.dumps(redact_value(compact), ensure_ascii=False, separators=(",", ":"))
            )
            return f"\n[状态] {serialized}"
        minimal = {"truncated": True} if metadata.get("truncated") else {}
        fallback = f"\n[状态] {json.dumps(minimal, ensure_ascii=False, separators=(',', ':'))}"
        return truncate_text(fallback, budget)[0] if budget is not None else fallback

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
        """调用工具声明的模型状态，失败时使用不泄露内部 metadata 的默认值。"""

        try:
            metadata = tool.model_metadata(output)
        except Exception:  # noqa: BLE001
            return {}
        return dict(metadata) if isinstance(metadata, dict) else {}

    @staticmethod
    def _model_metadata_priority(
        tool: Tool, output: ToolOutput, metadata: dict[str, Any]
    ) -> tuple[str, ...]:
        """读取工具声明的状态优先级，并过滤不存在字段避免生成无效状态。"""

        try:
            priority = tool.model_metadata_priority(output)
        except Exception:  # noqa: BLE001
            priority = tuple(metadata)
        if not isinstance(priority, tuple):
            priority = tuple(priority) if isinstance(priority, list) else tuple(metadata)
        return tuple(key for key in priority if key in metadata)

    @staticmethod
    def _audit_summary(tool: Tool, output: ToolOutput) -> str:
        """调用工具声明的结构化摘要，避免核心层按工具名称分支。"""

        try:
            return redact_text(tool.audit_summary(output))
        except Exception:  # noqa: BLE001
            state = "失败" if output.is_error else "成功"
            return f"{tool.name} {state}：结果摘要生成失败，未保存结果正文"

    @staticmethod
    def _display_arguments(tool: Tool | None, arguments: object) -> str:
        if tool is None:
            return redact_text(safe_argument_summary("unknown", arguments))
        try:
            return redact_text(tool.display_arguments(arguments))
        except Exception:  # noqa: BLE001
            return redact_text(safe_argument_summary(tool.name, arguments))

    @staticmethod
    def _tool_label(tool_name: str, tool: Tool | None) -> str:
        """为过程和未知工具错误生成不携带原始敏感标识的工具标签。"""

        if tool is not None:
            return tool.name
        return safe_audit_identifier(tool_name, "unknown-tool")

    def safe_tool_label(self, tool_name: str) -> str:
        """返回 Registry 确认的工具名，未知名称只返回安全标签。"""

        return self._tool_label(tool_name, self.registry.get(tool_name))

    def record_mode_confirmation(self, task_id: str) -> None:
        """在 full-access 启动确认后记录任务级人工确认。"""

        request = ApprovalRequest(
            action_type="full_access_session",
            summary="任务级完全访问确认",
            audit_summary=f"任务审查模式={self.review_mode.value}，启动确认已通过",
        )
        try:
            self.audit_repository.record_approval(
                task_id,
                "__task__",
                request.action_type,
                request.audit_summary,
                True,
                "human",
            )
        except Exception as exc:
            raise AuditError(
                f"完全访问确认审计写入失败：任务 {task_id}，原因：{type(exc).__name__}"
            ) from exc

    def _emit(self, event_type: str, task_id: str, message: str) -> None:
        emit_safely(self.event_sink, RuntimeEvent(event_type, task_id, redact_text(message)))

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
