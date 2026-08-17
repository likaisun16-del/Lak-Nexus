"""Agent Loop：驱动模型与 ToolExecutor 串行交互，不直接访问文件、进程或数据库。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from ..errors import TaskAlreadyExistsError
from ..executor.service import ToolExecutor
from ..models.base import ModelBackend
from ..safety.redaction import redact_text
from .schemas import (
    AgentResult,
    ChatMessage,
    TaskStatus,
)

_SYSTEM_PROMPT = """你是立凯中枢本地智能体。只能通过提供的四个工具完成工作：read、write、edit、bash。
遵守工作区路径限制、人工审批和命令策略；工具返回错误时先理解具体报错点再决定是否调整。
如果不需要工具，直接用简洁中文回答用户。"""


class TaskStateStore(Protocol):
    """Agent Loop 使用的任务状态协议，隔离编排层与 SQLite 仓储实现。"""

    def create(self, task_id: str, request_text: str) -> bool:
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


class AgentLoop:
    """最小模型循环，工具按模型返回顺序串行执行。"""

    def __init__(
        self,
        backend: ModelBackend,
        executor: ToolExecutor,
        task_store: TaskStateStore,
        max_turns: int = 20,
    ) -> None:
        self.backend = backend
        self.executor = executor
        self.task_store = task_store
        self.max_turns = max_turns

    async def run(
        self,
        request_text: str,
        *,
        task_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        """创建并执行一次任务，重复 task_id 直接失败而不覆盖旧记录。"""

        if not request_text.strip():
            raise ValueError("任务创建失败：request_text 不能为空")
        task_id = task_id or uuid.uuid4().hex
        if not self.task_store.create(task_id, request_text):
            raise TaskAlreadyExistsError(f"任务创建失败：task_id 已存在：{task_id}")
        self.task_store.set_status(task_id, TaskStatus.RUNNING)
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=request_text),
        ]
        cancel_event = cancel_event or asyncio.Event()
        try:
            return await self._run_turns(task_id, messages, cancel_event)
        except asyncio.CancelledError:
            message = "任务已取消：执行协程收到取消信号"
            self.task_store.set_status(
                task_id, TaskStatus.CANCELLED, error_type="CancelledError", error_message=message
            )
            return AgentResult(task_id, TaskStatus.CANCELLED, error_message=message)

    async def _run_turns(
        self, task_id: str, messages: list[ChatMessage], cancel_event: asyncio.Event
    ) -> AgentResult:
        for turn_number in range(1, self.max_turns + 1):
            if cancel_event.is_set():
                return self._cancel(task_id)
            try:
                turn = await self.backend.complete(messages, self.executor.registry.specs(), cancel_event)
            except asyncio.CancelledError:
                raise
            # Backend 边界统一记录模型调用失败，保留轮次和异常类型作为具体报错点。
            except Exception as exc:  # noqa: BLE001
                message = redact_text(f"模型调用失败：{type(exc).__name__}: {exc}")
                self.task_store.set_status(
                    task_id,
                    TaskStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=message,
                )
                return AgentResult(task_id, TaskStatus.FAILED, error_message=message, turns=turn_number)
            messages.append(
                ChatMessage(role="assistant", content=turn.content, tool_calls=turn.tool_calls)
            )
            if not turn.tool_calls:
                self.task_store.set_status(
                    task_id, TaskStatus.SUCCESS, result_summary=redact_text(turn.content)
                )
                return AgentResult(task_id, TaskStatus.SUCCESS, content=turn.content, turns=turn_number)
            for tool_call in turn.tool_calls:
                if cancel_event.is_set():
                    return self._cancel(task_id)
                result = await self.executor.execute(task_id, tool_call, cancel_event)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result.content,
                        tool_call_id=result.tool_call_id,
                        name=tool_call.name,
                    )
                )
        message = f"任务失败：模型交互轮数超过上限 {self.max_turns}，仍未返回最终答案"
        self.task_store.set_status(
            task_id, TaskStatus.FAILED, error_type="MaxTurnsExceeded", error_message=message
        )
        return AgentResult(task_id, TaskStatus.FAILED, error_message=message, turns=self.max_turns)

    def _cancel(self, task_id: str) -> AgentResult:
        message = "任务已取消：收到取消信号，未继续执行后续工具"
        self.task_store.set_status(
            task_id, TaskStatus.CANCELLED, error_type="TaskCancelled", error_message=message
        )
        return AgentResult(task_id, TaskStatus.CANCELLED, error_message=message)
