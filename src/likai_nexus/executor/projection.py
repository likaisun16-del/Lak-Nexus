"""工具投影服务：集中生成模型、界面事件和审计摘要所需的安全视图。"""

from __future__ import annotations

import json
from typing import Any

from ..safety.redaction import (
    redact_text,
    redact_value,
    sanitize_terminal_text,
    sanitize_terminal_value,
    truncate_text,
)
from ..tools.base import Tool, ToolOutput, safe_argument_summary
from ..tools.contracts import ToolDisplayField, ToolDisplayProjection


class ToolProjectionService:
    """隔离工具执行和各消费方投影，保证展示异常不会影响执行结果。"""

    def __init__(self, model_budget: int, display_command_bytes: int, display_result_bytes: int) -> None:
        self.model_budget = model_budget
        self.display_command_bytes = display_command_bytes
        self.display_result_bytes = display_result_bytes

    def model_content(
        self,
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
        status = self.status_envelope(safe_metadata, budget, metadata_priority)
        if budget is None:
            return content + status
        if len((content + status).encode("utf-8")) <= budget:
            return content + status
        body_budget = max(0, budget - len(status.encode("utf-8")))
        if len(content.encode("utf-8")) > body_budget and not safe_metadata.get("truncated"):
            safe_metadata = {**safe_metadata, "truncated": True}
            status = self.status_envelope(safe_metadata, budget, metadata_priority)
            body_budget = max(0, budget - len(status.encode("utf-8")))
        body = self.bounded_model_body(
            content,
            body_budget,
            bool(safe_metadata.get("truncated")),
        )
        return body + status

    @staticmethod
    def status_envelope(
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
    def bounded_model_body(content: str, budget: int, truncated: bool) -> str:
        """截取模型正文并尽量保留中文截断标记，状态信封负责最终可见性。"""

        if not truncated:
            return truncate_text(content, budget)[0]
        marker = "\n[输出已截断]"
        if len(marker.encode("utf-8")) > budget:
            return truncate_text(content, budget)[0]
        source = content.removesuffix(marker)
        prefix, _ = truncate_text(source, budget - len(marker.encode("utf-8")))
        return prefix + marker

    @staticmethod
    def model_metadata(tool: Tool, output: ToolOutput) -> dict[str, Any]:
        """读取工具声明的模型状态，失败时使用不泄露内部 metadata 的默认值。"""

        try:
            metadata = tool.model_metadata(output)
        except Exception:  # noqa: BLE001
            return {}
        return dict(metadata) if isinstance(metadata, dict) else {}

    @staticmethod
    def model_metadata_priority(
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
    def audit_summary(tool: Tool, output: ToolOutput) -> str:
        """调用工具声明的结构化摘要，避免核心层按工具名称分支。"""

        try:
            return redact_text(tool.audit_summary(output))
        except Exception:  # noqa: BLE001
            status = output.effective_status()
            return f"{tool.name} {status.label}：结果摘要生成失败，未保存结果正文"

    def display_arguments(self, tool: Tool | None, arguments: object) -> str:
        """生成脱敏且有界的过程展示摘要。"""

        try:
            value = safe_argument_summary("unknown", arguments) if tool is None else tool.display_arguments(arguments)
            return self.bounded_display_text(
                value,
                self.display_command_bytes,
                "\n[指令已截断]",
            )
        except Exception:  # noqa: BLE001
            return "[指令展示不可用]"

    def display_result(
        self, tool: Tool | None, output: ToolOutput | None
    ) -> ToolDisplayProjection:
        """把工具显示结果转换成通用字段，并在旁路异常时降级为空投影。"""

        try:
            if tool is None:
                return ToolDisplayProjection()
            value = tool.display_result(output)
            if isinstance(value, ToolDisplayProjection):
                projection = value
            elif isinstance(value, dict):
                projection = ToolDisplayProjection(
                    tuple(ToolDisplayField(str(key), item) for key, item in value.items())
                )
            else:
                return ToolDisplayProjection()
            safe_value = redact_value(sanitize_terminal_value(projection.as_dict()))
            raw_fields = safe_value.get("fields") if isinstance(safe_value, dict) else None
            if not isinstance(raw_fields, list):
                return ToolDisplayProjection()
            fields: list[ToolDisplayField] = []
            for raw_field in raw_fields:
                if not isinstance(raw_field, dict) or "label" not in raw_field:
                    continue
                field_value = raw_field.get("value")
                if isinstance(field_value, str):
                    field_value = self.bounded_display_text(
                        field_value,
                        self.display_result_bytes,
                        "\n[输出预览已截断]",
                    )
                fields.append(ToolDisplayField(str(raw_field["label"]), field_value))
            return ToolDisplayProjection(tuple(fields))
        except Exception:  # noqa: BLE001
            return ToolDisplayProjection()

    def event_metadata(
        self,
        tool: Tool | None,
        status: str,
        elapsed_ms: int,
        reason: str | None,
        display: ToolDisplayProjection,
    ) -> dict[str, Any]:
        """生成与具体工具无关的运行事件结果字段。"""

        metadata: dict[str, Any] = {
            "tool_name": tool.name if tool is not None else "unknown",
            "status": status,
            "elapsed_ms": elapsed_ms,
        }
        if reason:
            metadata["reason"] = self.short_reason(reason)
        if display.fields:
            metadata["result"] = display.as_dict()
        return metadata

    @staticmethod
    def short_reason(reason: str) -> str:
        """保留一行、脱敏且有界的事件原因。"""

        safe_reason = redact_text(sanitize_terminal_text(reason))
        first_line = safe_reason.splitlines()[0] if safe_reason.splitlines() else safe_reason
        return truncate_text(first_line, 240)[0]

    @staticmethod
    def bounded_display_text(value: str, limit: int, marker: str) -> str:
        """清理终端控制字符和敏感值，并限制展示字段字节数。"""

        safe_value = redact_text(sanitize_terminal_text(str(value)))
        marker_bytes = len(marker.encode("utf-8"))
        if marker_bytes >= limit:
            return truncate_text(marker, limit)[0]
        bounded, truncated = truncate_text(safe_value, limit - marker_bytes)
        return bounded + marker if truncated else bounded
