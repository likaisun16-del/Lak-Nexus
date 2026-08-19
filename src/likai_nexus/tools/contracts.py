"""工具公共契约：连接模型、工具目录、执行器、展示端和审计存储。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """提供给模型的工具名称、描述和 JSON Schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求的一次结构化工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


class ToolStatus(StrEnum):
    """工具执行的标准终态，供模型、展示和审计投影共同引用。"""

    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    @property
    def is_error(self) -> bool:
        """判断该终态是否应作为工具错误回填模型。"""

        return self is not ToolStatus.SUCCESS

    @property
    def label(self) -> str:
        """返回审计和界面摘要使用的稳定中文状态标签。"""

        return {
            ToolStatus.SUCCESS: "成功",
            ToolStatus.FAILED: "失败",
            ToolStatus.REJECTED: "拒绝",
            ToolStatus.TIMEOUT: "超时",
            ToolStatus.CANCELLED: "取消",
        }[self]


@dataclass(frozen=True, slots=True)
class ToolDisplayField:
    """通用界面字段，不绑定 Bash 或其他具体工具名称。"""

    label: str
    value: Any


@dataclass(frozen=True, slots=True)
class ToolDisplayProjection:
    """供 CLI、未来 Channel 或 Web UI 消费的通用展示投影。"""

    fields: tuple[ToolDisplayField, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """转换为已经过执行器安全处理的事件 metadata 结构。"""

        return {
            "fields": [
                {"label": field.label, "value": field.value} for field in self.fields
            ]
        }


@dataclass(frozen=True, slots=True)
class ToolModelProjection:
    """回填模型的正文和状态，和界面、审计投影保持隔离。"""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolAuditProjection:
    """工具调用在审计中的参数摘要和结果摘要。"""

    arguments_summary: str
    result_summary: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的结构化结果，明确区分执行、模型、展示和审计投影。"""

    tool_call_id: str
    status: ToolStatus
    model: ToolModelProjection
    display: ToolDisplayProjection = field(default_factory=ToolDisplayProjection)
    audit: ToolAuditProjection | None = None

    @property
    def content(self) -> str:
        """兼容旧调用方读取模型正文。"""

        return self.model.content

    @property
    def is_error(self) -> bool:
        """兼容旧调用方读取工具是否失败。"""

        return self.status.is_error

    @property
    def metadata(self) -> dict[str, Any]:
        """兼容旧调用方读取模型状态 metadata。"""

        return self.model.metadata

    @property
    def display_metadata(self) -> dict[str, Any]:
        """兼容旧调用方读取通用界面投影。"""

        return self.display.as_dict()
