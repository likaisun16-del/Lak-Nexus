"""中立运行事件契约：供 Agent Loop、Executor、CLI 和未来 Channel 共同消费。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """一次可展示的安全运行事件，不承载模型隐藏推理或原始敏感参数。"""

    event_type: str
    task_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


class EventSink(Protocol):
    """过程事件接收协议，展示端故障不能改变任务结果。"""

    def emit(self, event: RuntimeEvent) -> None:
        """接收一条已经脱敏的运行事件。"""


class NullEventSink:
    """关闭过程输出时使用的空事件接收器。"""

    def emit(self, event: RuntimeEvent) -> None:
        return


def emit_safely(sink: EventSink, event: RuntimeEvent) -> None:
    """隔离展示端异常，避免输出故障把成功任务改成失败。"""

    try:
        sink.emit(event)
    except Exception:  # noqa: BLE001
        return
