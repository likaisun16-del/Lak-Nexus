"""测试模型后端：按预设顺序返回响应，验证 Agent Loop 而不触发网络调用。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ..errors import ModelBackendError
from ..orchestrator.schemas import AssistantTurn, ChatMessage, ToolSpec


class FakeModelBackend:
    """可脚本化的 Fake Backend，适用于单元测试和最小集成测试。"""

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.call_count = 0
        self.messages: list[tuple[ChatMessage, ...]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        cancel_event: asyncio.Event | None = None,
    ) -> AssistantTurn:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        self.messages.append(tuple(messages))
        if self.call_count >= len(self._turns):
            raise ModelBackendError("Fake 模型失败：预设响应已耗尽")
        turn = self._turns[self.call_count]
        self.call_count += 1
        return turn
