"""审批接口：将人工确认从命令策略和具体工具中解耦，便于 CLI 与测试复用。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .redaction import redact_text, sanitize_terminal_text, truncate_text


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """一次高风险操作的最小展示信息。"""

    action_type: str
    summary: str
    fingerprint: str = ""
    audit_summary: str = ""
    confirmation_token: str | None = None


class ApprovalHandler(Protocol):
    """审批实现只负责给出允许或拒绝，不判断命令风险。"""

    async def request(self, request: ApprovalRequest) -> bool:
        """请求用户决定。"""


class CliApprovalHandler:
    """本地 CLI 审批：在工作线程中读取输入，避免阻塞事件循环。"""

    _PROMPT_FIELD_BYTES = 4096
    _PROMPT_TRUNCATION_MARKER = " [审批摘要已截断]"

    @classmethod
    def _safe_prompt_field(
        cls, value: object, fallback: str, *, truncation_marker: str = ""
    ) -> tuple[str, bool]:
        """在 input() 边界清理不可信审批字段，不改变审批绑定的原始值。"""

        try:
            text = redact_text(sanitize_terminal_text(str(value)))
            single_line = " ".join(text.splitlines())
            marker_bytes = len(truncation_marker.encode("utf-8"))
            if truncation_marker and marker_bytes < cls._PROMPT_FIELD_BYTES:
                bounded, truncated = truncate_text(
                    single_line, cls._PROMPT_FIELD_BYTES - marker_bytes
                )
                return (
                    bounded + truncation_marker if truncated else bounded,
                    truncated,
                )
            bounded, truncated = truncate_text(single_line, cls._PROMPT_FIELD_BYTES)
            return bounded, truncated
        except Exception:  # noqa: BLE001
            return fallback, False

    async def request(self, request: ApprovalRequest) -> bool:
        action_type, _ = self._safe_prompt_field(request.action_type, "unknown-action")
        summary, _ = self._safe_prompt_field(
            request.summary,
            "审批摘要不可用",
            truncation_marker=self._PROMPT_TRUNCATION_MARKER,
        )
        if request.confirmation_token:
            confirmation_token, _ = self._safe_prompt_field(
                request.confirmation_token, "确认令牌不可用"
            )
            prompt = (
                f"\n需要强确认 [{action_type}]：{summary}\n"
                f"请输入 {confirmation_token}："
            )
        else:
            prompt = f"\n需要审批 [{action_type}]：{summary}\n允许执行？[y/N] "
        answer = await asyncio.to_thread(input, prompt)
        if request.confirmation_token:
            return answer.strip() == request.confirmation_token
        return answer.strip().lower() in {"y", "yes", "是", "允许"}


class StaticApprovalHandler:
    """测试用固定审批结果。"""

    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests: list[ApprovalRequest] = []

    async def request(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.approved
