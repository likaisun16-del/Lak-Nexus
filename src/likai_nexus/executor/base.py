"""工具协议：约束四个具体工具的参数校验、安全检查、审批和执行接口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..orchestrator.schemas import ToolSpec
from ..safety.approval import ApprovalRequest


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """具体工具的内部结果，之后由 ToolExecutor 转换成公共 ToolResult。"""

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    """四个工具共同遵循的协议，禁止工具绕过执行器直接对外暴露。"""

    name: str
    spec: ToolSpec

    def validate(self, arguments: object) -> dict[str, Any]:
        """校验并归一化模型参数。"""

    def check_safety(self, arguments: dict[str, Any]) -> None:
        """执行路径或命令安全检查。"""

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        """返回需要人工确认的请求；只读工具返回 None。"""

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        """在 ToolExecutor 已完成安全检查后执行具体动作。"""
