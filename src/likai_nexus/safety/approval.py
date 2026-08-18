"""审批接口：将人工确认从命令策略和具体工具中解耦，便于 CLI 与测试复用。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


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

    async def request(self, request: ApprovalRequest) -> bool:
        if request.confirmation_token:
            prompt = (
                f"\n需要强确认 [{request.action_type}]：{request.summary}\n"
                f"请输入 {request.confirmation_token}："
            )
        else:
            prompt = f"\n需要审批 [{request.action_type}]：{request.summary}\n允许执行？[y/N] "
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
