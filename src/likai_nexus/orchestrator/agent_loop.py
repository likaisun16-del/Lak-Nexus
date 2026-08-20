"""Agent Loop：驱动模型与 ToolExecutor 串行交互，不直接访问文件、进程或数据库。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..errors import ConfigError, TaskAlreadyExistsError
from ..events import EventSink, RuntimeEvent, emit_safely
from ..executor.service import ToolExecutor
from ..models.base import ModelBackend
from ..safety.approval import ApprovalHandler, ApprovalRequest
from ..safety.redaction import (
    redact_text,
    redact_value,
    sanitize_terminal_text,
    sanitize_terminal_value,
)
from ..safety.review_mode import ReviewMode, parse_review_mode
from .schemas import (
    AgentResult,
    ChatMessage,
    TaskStatus,
)

_SYSTEM_PROMPT = """你是立凯中枢本地智能体。只能通过当前请求提供的工具完成工作：{tool_names}。
当前审查模式是 {review_mode}：{mode_guidance}
需要生成、复用或维护脚本时，默认将脚本保存到工作区内的 script/ 目录；Bash 仍从工作区根目录执行。
工具返回错误时先理解具体报错点再决定是否调整。
如果不需要工具，直接用简洁中文回答用户。"""


class TaskStateStore(Protocol):
    """Agent Loop 使用的任务状态协议，隔离编排层与 SQLite 仓储实现。"""

    def create(
        self,
        task_id: str,
        request_text: str,
        review_mode: ReviewMode = ReviewMode.STRICT,
    ) -> bool:
        """创建任务并返回是否成功，重复 ID 必须返回 False。"""

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_summary: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """持久化任务状态和可诊断摘要。"""

    def get(self, task_id: str) -> dict[str, Any] | None:
        """读取任务，供上层在创建可见消息前执行幂等预检。"""


class AgentLoop:
    """最小模型循环，工具按模型返回顺序串行执行。"""

    def __init__(
        self,
        backend: ModelBackend,
        executor: ToolExecutor,
        task_store: TaskStateStore,
        max_turns: int = 50,
        *,
        review_mode: ReviewMode | str | None = None,
        approvals: ApprovalHandler | None = None,
        event_sink: EventSink | None = None,
        full_access_confirmed: bool = False,
        on_full_access_confirmed: Callable[[], None] | None = None,
    ) -> None:
        self.backend = backend
        self.executor = executor
        self.task_store = task_store
        self.max_turns = max_turns
        executor_mode = executor.review_mode
        requested_mode = (
            executor_mode if review_mode is None else parse_review_mode(review_mode)
        )
        if requested_mode is not executor_mode:
            raise ConfigError(
                "AgentLoop 配置失败：审查模式 "
                f"{requested_mode.value} 与 ToolExecutor 的审查模式 {executor_mode.value} 不一致"
            )
        self.review_mode = executor_mode
        self.approvals = approvals if approvals is not None else executor.approvals
        self.event_sink = event_sink if event_sink is not None else executor.event_sink
        self.full_access_confirmed = full_access_confirmed
        self.on_full_access_confirmed = on_full_access_confirmed

    async def run(
        self,
        request_text: str,
        *,
        task_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        context_messages: Sequence[ChatMessage] | None = None,
    ) -> AgentResult:
        """创建并执行一次任务；可接收由 Session 预先解析的可见分支历史。"""

        if not request_text.strip():
            raise ValueError("任务创建失败：request_text 不能为空")
        task_id = task_id or uuid.uuid4().hex
        confirmation_source = "preference"
        if self.review_mode is ReviewMode.FULL_ACCESS and not self.full_access_confirmed:
            confirmed = await self._confirm_full_access(task_id)
            if not confirmed:
                return AgentResult(
                    task_id,
                    TaskStatus.CANCELLED,
                    error_message="任务未创建：完全访问模式未通过启动确认",
                )
            try:
                if self.on_full_access_confirmed is not None:
                    self.on_full_access_confirmed()
            except Exception as exc:  # noqa: BLE001
                message = redact_text(
                    f"任务未创建：完全访问确认已通过，但本地偏好保存失败：{type(exc).__name__}: {exc}"
                )
                self._emit("task_finished", task_id, f"任务取消：{message}")
                return AgentResult(task_id, TaskStatus.CANCELLED, error_message=message)
            self.full_access_confirmed = True
            confirmation_source = "human"
        if not self.task_store.create(task_id, request_text, self.review_mode):
            raise TaskAlreadyExistsError(f"任务创建失败：task_id 已存在：{task_id}")
        if self.review_mode is ReviewMode.FULL_ACCESS:
            try:
                self.executor.record_mode_confirmation(task_id, confirmation_source)
            except Exception as exc:  # noqa: BLE001
                message = redact_text(f"任务启动失败：完全访问确认审计失败：{type(exc).__name__}: {exc}")
                self.task_store.set_status(
                    task_id,
                    TaskStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=message,
                )
                self._emit("task_finished", task_id, f"任务失败：总轮数=0，{message}")
                return AgentResult(task_id, TaskStatus.FAILED, error_message=message)
        self.task_store.set_status(task_id, TaskStatus.RUNNING)
        self._emit(
            "task_started",
            task_id,
            f"任务开始：task_id={task_id}，审查模式={self.review_mode.value}，"
            f"{self._mode_start_notice()}",
        )
        tool_names = ", ".join(spec.name for spec in self.executor.registry.specs()) or "无"
        messages = [
            ChatMessage(
                role="system",
                content=_SYSTEM_PROMPT.format(
                    tool_names=tool_names,
                    review_mode=self.review_mode.value,
                    mode_guidance=self._mode_guidance(),
                ),
            )
        ]
        messages.extend(context_messages or ())
        messages.append(ChatMessage(role="user", content=request_text))
        cancel_event = cancel_event or asyncio.Event()
        try:
            return await self._run_turns(task_id, messages, cancel_event)
        except asyncio.CancelledError:
            message = "任务已取消：总轮数=0，执行协程收到取消信号"
            self.task_store.set_status(
                task_id, TaskStatus.CANCELLED, error_type="CancelledError", error_message=message
            )
            self._emit("task_finished", task_id, f"任务取消：{message}")
            return AgentResult(task_id, TaskStatus.CANCELLED, error_message=message)
        except Exception as exc:  # noqa: BLE001
            message = redact_text(f"任务执行失败：总轮数=0，{type(exc).__name__}: {exc}")
            self._mark_failed(task_id, type(exc).__name__, message)
            self._emit("task_finished", task_id, f"任务失败：{message}")
            return AgentResult(task_id, TaskStatus.FAILED, error_message=message)

    async def _run_turns(
        self, task_id: str, messages: list[ChatMessage], cancel_event: asyncio.Event
    ) -> AgentResult:
        for turn_number in range(1, self.max_turns + 1):
            if cancel_event.is_set():
                return self._cancel(task_id, turn_number - 1)
            self._emit(
                "model_started",
                task_id,
                f"模型调用开始：轮次 {turn_number}",
                {
                    "turn_number": turn_number,
                    "max_turns": self.max_turns,
                    "status": "started",
                },
            )
            try:
                turn = await self.backend.complete(messages, self.executor.registry.specs(), cancel_event)
            except asyncio.CancelledError:
                return self._cancel(task_id, turn_number)
            # Backend 边界统一记录模型调用失败，保留轮次和异常类型作为具体报错点。
            except Exception as exc:  # noqa: BLE001
                message = redact_text(f"模型调用失败：{type(exc).__name__}: {exc}")
                self.task_store.set_status(
                    task_id,
                    TaskStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=message,
                )
                self._emit(
                    "model_failed",
                    task_id,
                    f"模型调用失败：轮次 {turn_number}，{message}",
                    {
                        "turn_number": turn_number,
                        "max_turns": self.max_turns,
                        "status": "failed",
                        "reason": message,
                    },
                )
                self._emit(
                    "task_finished",
                    task_id,
                    f"任务失败：总轮数={turn_number}，{message}",
                )
                return AgentResult(task_id, TaskStatus.FAILED, error_message=message, turns=turn_number)
            safe_content = redact_text(turn.content)
            self._emit(
                "model_finished",
                task_id,
                f"模型调用结束：轮次 {turn_number}，工具调用数={len(turn.tool_calls)}",
                {
                    "turn_number": turn_number,
                    "max_turns": self.max_turns,
                    "status": "finished",
                    "tool_call_count": len(turn.tool_calls),
                },
            )
            messages.append(
                ChatMessage(role="assistant", content=safe_content, tool_calls=turn.tool_calls)
            )
            if not turn.tool_calls:
                self.task_store.set_status(
                    task_id, TaskStatus.SUCCESS, result_summary=safe_content
                )
                self._emit(
                    "task_finished",
                    task_id,
                    f"任务完成：状态=success，总轮数={turn_number}",
                )
                return AgentResult(task_id, TaskStatus.SUCCESS, content=safe_content, turns=turn_number)
            for tool_call in turn.tool_calls:
                if cancel_event.is_set():
                    return self._cancel(task_id, turn_number)
                try:
                    result = await self.executor.execute(task_id, tool_call, cancel_event)
                except asyncio.CancelledError:
                    return self._cancel(task_id, turn_number)
                except Exception as exc:  # noqa: BLE001
                    tool_label = self.executor.safe_tool_label(tool_call.name)
                    message = redact_text(
                        f"工具调用失败：轮次 {turn_number}，工具 {tool_label}，"
                        f"原因：{type(exc).__name__}: {exc}"
                    )
                    self._mark_failed(task_id, type(exc).__name__, message)
                    self._emit(
                        "task_finished",
                        task_id,
                        f"任务失败：总轮数={turn_number}，{message}",
                    )
                    return AgentResult(
                        task_id, TaskStatus.FAILED, error_message=message, turns=turn_number
                    )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result.content,
                        tool_call_id=result.tool_call_id,
                        name=tool_call.name,
                    )
                )
        message = (
            f"任务失败：总轮数={self.max_turns}，模型交互轮数超过上限 {self.max_turns}，"
            "仍未返回最终答案"
        )
        self.task_store.set_status(
            task_id, TaskStatus.FAILED, error_type="MaxTurnsExceeded", error_message=message
        )
        self._emit("task_finished", task_id, f"任务失败：{message}")
        return AgentResult(task_id, TaskStatus.FAILED, error_message=message, turns=self.max_turns)

    def _mark_failed(self, task_id: str, error_type: str, message: str) -> None:
        """尽力把未预期异常落为 failed，避免任务长期停留在 running。"""

        try:
            self.task_store.set_status(
                task_id, TaskStatus.FAILED, error_type=error_type, error_message=message
            )
        except Exception:  # noqa: BLE001
            # 原始错误已经返回给调用方；状态库故障不能再次覆盖该报错点。
            return

    def _cancel(self, task_id: str, turns: int = 0) -> AgentResult:
        message = f"任务已取消：总轮数={turns}，收到取消信号，未继续执行后续工具"
        self.task_store.set_status(
            task_id, TaskStatus.CANCELLED, error_type="TaskCancelled", error_message=message
        )
        self._emit("task_finished", task_id, f"任务取消：{message}")
        return AgentResult(task_id, TaskStatus.CANCELLED, error_message=message, turns=turns)

    async def _confirm_full_access(self, task_id: str) -> bool:
        """在创建任务和调用模型前完成一次不可静默跳过的完全访问确认。"""

        if self.approvals is None:
            self._emit(
                "mode_denied",
                task_id,
                "完全访问模式拒绝：未配置启动确认处理器",
            )
            return False
        request = ApprovalRequest(
            action_type="full_access_session",
            summary=(
                "完全访问将解除工作区、敏感路径和 Bash 允许列表限制；"
                "请输入 FULL-ACCESS 确认继续"
            ),
            confirmation_token="FULL-ACCESS",
        )
        try:
            approved = await self.approvals.request(request)
        except (EOFError, KeyboardInterrupt):
            approved = False
        self._emit(
            "mode_confirmed" if approved else "mode_denied",
            task_id,
            "完全访问模式启动确认通过" if approved else "完全访问模式启动确认拒绝",
        )
        return approved

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

    def _mode_guidance(self) -> str:
        if self.review_mode is ReviewMode.STRICT:
            return "只访问工作区内非敏感路径，写入、修改和 Bash 必须等待人工审批。"
        if self.review_mode is ReviewMode.RELAXED:
            return "文件仍限于工作区非敏感路径；原始 Shell 脚本每次执行前必须等待人工审批。"
        return "任务已完成完全访问强确认；仍受当前操作系统权限、超时、取消和输出限制约束。"

    def _mode_start_notice(self) -> str:
        if self.review_mode is ReviewMode.RELAXED:
            return "风险提示：原始 Shell 每次执行前都需要人工审批"
        if self.review_mode is ReviewMode.FULL_ACCESS:
            return "风险提示：任务级完全访问确认已通过"
        return "严格模式：写入、修改和 Bash 操作需要人工审批"
