"""write 工具：由 ToolExecutor 审批后在 WorkspacePathResolver 范围内原子写入 UTF-8 文本。"""

from __future__ import annotations

import asyncio
from typing import Any

from ...errors import ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.paths import ResolvedPath, WorkspacePathResolver
from ...safety.redaction import action_fingerprint, content_sha256, redact_text, truncate_text
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
        content = arguments["content"]
        content_bytes = len(content.encode("utf-8"))
        content_hash = content_sha256(content)
        target_hash = self._target_hash(resolved)
        preview, preview_truncated = truncate_text(redact_text(content), 240)
        action_data = {
            "path": resolved.relative_path,
            "action": action,
            "content_sha256": content_hash,
            "target_sha256": target_hash,
        }
        return ApprovalRequest(
            action_type=f"write_{'overwrite' if resolved.exists else 'create'}",
            summary=(
                f"{action}工作区文件 {resolved.relative_path}，写入 {content_bytes} 字节，"
                f"内容 sha256={content_hash}，预览={preview!r}"
                f"{'（预览已截断）' if preview_truncated else ''}"
            ),
            fingerprint=action_fingerprint(action_data),
            audit_summary=(
                f"{action} {resolved.relative_path}：字节数={content_bytes}，"
                f"内容 sha256={content_hash}，目标 sha256={target_hash}"
            ),
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

    @staticmethod
    def _target_hash(resolved: ResolvedPath) -> str | None:
        """读取审批时的旧文件摘要，执行前变化会使审批指纹失效。"""

        if not resolved.exists:
            return None
        try:
            return content_sha256(resolved.path.read_bytes())
        except OSError as exc:
            return f"[读取失败:{type(exc).__name__}]"
