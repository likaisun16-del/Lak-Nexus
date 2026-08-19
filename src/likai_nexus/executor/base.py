"""工具契约：约束显式注册工具的参数校验、安全检查、审批和执行接口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..orchestrator.schemas import ToolSpec
from ..safety.approval import ApprovalRequest
from ..safety.redaction import safe_audit_identifier
from ..safety.review_mode import ReviewMode


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """具体工具的内部结果，之后由 ToolExecutor 转换成公共 ToolResult。"""

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    display_metadata: dict[str, Any] = field(default_factory=dict)


def safe_argument_summary(tool_name: str, arguments: object) -> str:
    """生成不包含参数值的保守摘要，供未声明展示/审计能力的工具使用。"""

    if not isinstance(arguments, dict):
        return f"工具 {tool_name} 参数摘要：类型={type(arguments).__name__}"
    fields = sorted(
        f"{safe_audit_identifier(key, 'field')}:{type(value).__name__}"
        for key, value in arguments.items()
    )
    return f"工具 {tool_name} 参数摘要：字段数量={len(fields)}，字段摘要={fields}"


class Tool:
    """工具基类：统一执行契约，并为新工具提供保守的默认安全摘要。"""

    name: str
    spec: ToolSpec
    review_mode: ReviewMode | None = None

    def validate(self, arguments: object) -> dict[str, Any]:
        """校验并归一化模型参数。"""

        raise NotImplementedError

    def check_safety(self, arguments: dict[str, Any]) -> None:
        """执行路径或命令安全检查。"""

        raise NotImplementedError

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        """返回需要人工确认的请求；只读工具返回 None。"""

        raise NotImplementedError

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        """在 ToolExecutor 已完成安全检查后执行具体动作。"""

        raise NotImplementedError

    def display_arguments(self, arguments: object) -> str:
        """返回过程展示摘要；默认不输出任何参数值。"""

        return safe_argument_summary(self.name, arguments)

    def display_result(self, output: ToolOutput | None) -> dict[str, Any]:
        """返回界面结果投影；默认不暴露工具结果正文或内部 metadata。"""

        return {}

    def audit_arguments(self, arguments: object) -> str:
        """返回持久化参数摘要；默认只保存字段名和类型。"""

        return safe_argument_summary(self.name, arguments)

    def model_metadata(self, output: ToolOutput) -> dict[str, Any]:
        """返回允许回填模型的安全状态；默认不暴露内部 metadata。"""

        return {}

    def model_metadata_priority(self, output: ToolOutput) -> tuple[str, ...]:
        """声明预算紧张时保留模型状态字段的顺序；默认沿用状态字典顺序。"""

        return tuple(self.model_metadata(output))

    def audit_summary(self, output: ToolOutput) -> str:
        """返回结果审计摘要；默认不保存结果正文或 metadata 值。"""

        state = "失败" if output.is_error else "成功"
        fields = sorted(safe_audit_identifier(key, "field") for key in output.metadata)
        return f"{self.name} {state}：未声明结果摘要，metadata字段摘要={fields}"

    def validate_runtime(self) -> None:
        """执行工具专属的启动检查；默认工具无需额外检查。"""

        return
