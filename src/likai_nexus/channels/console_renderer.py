"""CLI 过程渲染器：消费通用 RuntimeEvent，不按具体 Tool 名称分支。"""

from __future__ import annotations

import sys
import textwrap
from typing import ClassVar, TextIO

from ..events import RuntimeEvent
from ..safety.redaction import redact_text, sanitize_terminal_text, truncate_text


class ConsoleEventSink:
    """把已经安全处理的结构化事件投影为普通用户可读的过程行。"""

    _VISIBLE_EVENTS: ClassVar[set[str]] = {
        "task_started",
        "model_started",
        "model_failed",
        "tool_started",
        "tool_finished",
        "tool_failed",
        "tool_timed_out",
        "tool_rejected",
        "tool_cancelled",
    }

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def emit(self, event: RuntimeEvent) -> None:
        if event.event_type not in self._VISIBLE_EVENTS:
            return
        if event.event_type == "task_started":
            print(f"[任务] {event.message}", file=self.stream, flush=True)
            return
        if event.event_type in {"model_started", "model_failed"}:
            self._emit_model_event(event)
            return
        self._emit_tool_event(event)

    def _emit_tool_event(self, event: RuntimeEvent) -> None:
        tool_name = event.metadata.get("tool_name")
        status = event.metadata.get("status")
        if not isinstance(tool_name, str) or not isinstance(status, str):
            print(f"[工具] {event.message}", file=self.stream, flush=True)
            return
        if status == "started":
            invocation = event.metadata.get("invocation")
            if isinstance(invocation, str) and invocation:
                print(f"[工具] {tool_name}：执行指令", file=self.stream, flush=True)
                print(
                    textwrap.indent(self._safe_text(invocation), "  "),
                    file=self.stream,
                    flush=True,
                )
            else:
                print(f"[工具] {tool_name}：开始", file=self.stream, flush=True)
            return
        elapsed = event.metadata.get("elapsed_ms", "?")
        message = f"{tool_name}：{self._status_label(status)}（{elapsed}ms）"
        reason = event.metadata.get("reason")
        if reason and status in {"failed", "rejected", "cancelled", "timeout"}:
            message += f"，{self._short_text(reason)}"
        print(f"[工具] {message}", file=self.stream, flush=True)
        self._emit_result(event.metadata.get("result"))

    def _emit_model_event(self, event: RuntimeEvent) -> None:
        turn_number = event.metadata.get("turn_number")
        max_turns = event.metadata.get("max_turns")
        if not isinstance(turn_number, int) or not isinstance(max_turns, int):
            return
        status = event.metadata.get("status")
        if event.event_type == "model_started" and status == "started":
            message = f"第 {turn_number}/{max_turns} 轮：处理中"
        elif event.event_type == "model_failed" and status == "failed":
            reason = event.metadata.get("reason")
            message = f"第 {turn_number}/{max_turns} 轮：失败"
            if reason:
                message += f"，{self._short_text(reason)}"
        else:
            return
        print(f"[模型] {message}", file=self.stream, flush=True)

    def _emit_result(self, result: object) -> None:
        fields = self._fields(result)
        for field in fields:
            label = self._safe_text(field.get("label", "字段"))
            value = field.get("value")
            if value is None:
                print(f"  {label}=不可用", file=self.stream, flush=True)
                continue
            if isinstance(value, str) and "\n" in value:
                print(f"  {label}:", file=self.stream, flush=True)
                print(textwrap.indent(self._safe_text(value), "    "), file=self.stream, flush=True)
                continue
            print(f"  {label}={self._safe_text(value)}", file=self.stream, flush=True)

    @staticmethod
    def _fields(result: object) -> list[dict[str, object]]:
        if not isinstance(result, dict):
            return []
        fields = result.get("fields")
        if isinstance(fields, list):
            return [field for field in fields if isinstance(field, dict)]
        # 兼容旧事件结构，同时保持渲染端只消费通用键值字段。
        return [{"label": key, "value": value} for key, value in result.items()]

    @staticmethod
    def _safe_text(value: object) -> str:
        return redact_text(sanitize_terminal_text(str(value)))

    @classmethod
    def _short_text(cls, value: object) -> str:
        single_line = " ".join(cls._safe_text(value).splitlines())
        return truncate_text(single_line, 240)[0]

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "success": "成功",
            "failed": "失败",
            "timeout": "超时",
            "rejected": "拒绝",
            "cancelled": "取消",
        }.get(status, status)
