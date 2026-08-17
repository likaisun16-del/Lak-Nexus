"""write 工具：经审批后在工作区内创建或原子覆盖 UTF-8 文本文件。"""

from __future__ import annotations

import asyncio
from typing import Any

from ...errors import ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.paths import ResolvedPath, WorkspacePathResolver
from ..base import ToolOutput
from .common import atomic_write, require_arguments, require_string


class WriteFileTool:
    """完整写入工具，不保存或返回文件正文，避免扩大敏感内容传播范围。"""

    name = "write"
    spec = ToolSpec(
        name=name,
        description="创建或完整覆盖工作区内的 UTF-8 文本文件。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区路径"},
                "content": {"type": "string", "description": "要写入的完整文本"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        self.resolver = resolver

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        path = require_string(values, "path", self.name)
        content = values.get("content")
        if not isinstance(content, str):
            raise ValidationError("工具 write 参数校验失败：content 必须是字符串")
        return {"path": path, "content": content}

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self._resolve(arguments)

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest:
        resolved = self._resolve(arguments)
        action = "覆盖" if resolved.exists else "新建"
        return ApprovalRequest(
            action_type=f"write_{'overwrite' if resolved.exists else 'create'}",
            summary=f"{action}工作区文件 {resolved.relative_path}，写入 {len(arguments['content'].encode('utf-8'))} 字节",
        )

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        resolved = self._resolve(arguments)
        if cancel_event and cancel_event.is_set():
            return ToolOutput("写入已取消：收到任务取消信号", is_error=True, metadata={"cancelled": True})
        resolved.path.parent.mkdir(parents=True, exist_ok=True)
        data = arguments["content"].encode("utf-8")
        atomic_write(resolved.path, data, resolved.relative_path)
        action = "覆盖" if resolved.exists else "创建"
        return ToolOutput(
            content=f"已{action}文件：{resolved.relative_path}",
            metadata={"path": resolved.relative_path, "bytes": len(data), "action": action},
        )

    def _resolve(self, arguments: dict[str, Any]) -> ResolvedPath:
        return self.resolver.resolve(
            arguments["path"], file_only=True, reject_symlink=True
        )
