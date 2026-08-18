"""模型后端接口：隔离 Agent Loop 与 OpenAI 等具体供应商协议。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from ..orchestrator.schemas import AssistantTurn, ChatMessage, ToolSpec


class ModelBackend(Protocol):
    """模型后端必须实现的最小异步接口。"""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        cancel_event: asyncio.Event | None = None,
    ) -> AssistantTurn:
        """根据上下文和工具定义返回统一模型响应。"""
