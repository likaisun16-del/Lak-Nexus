"""公共数据契约：连接 CLI、模型、Agent Loop、工具执行器和任务存储。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..tools.contracts import ToolCall


class TaskStatus(StrEnum):
    """任务生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """模型上下文消息，同时保留 assistant 的工具调用以支持下一轮请求。"""

    role: str
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """模型单轮响应，供应商类型在转换后不得越过该契约。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentResult:
    """一次任务执行的最终结果，供 CLI 展示和测试断言。"""

    task_id: str
    status: TaskStatus
    content: str = ""
    error_message: str | None = None
    turns: int = 0
