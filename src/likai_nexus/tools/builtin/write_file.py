"""write 工具：由 ToolExecutor 审批后在 WorkspacePathResolver 范围内原子写入 UTF-8 文本。"""

from __future__ import annotations

import asyncio
from typing import Any

from ...errors import ValidationError
from ...safety.approval import ApprovalRequest
from ...safety.paths import ResolvedPath, WorkspacePathResolver
from ...safety.redaction import action_fingerprint, content_sha256, redact_text, truncate_text
from ..base import Tool, ToolOutput
from ..context import ToolExecutionContext
from ..contracts import ToolSpec
from .common import atomic_write, require_arguments, require_string


class WriteFileTool(Tool):
    """完整写入工具，不保存或返回文件正文，避免扩大敏感内容传播范围。"""

    name = "write"
    spec = ToolSpec(
        name=name,
        description="创建或完整覆盖当前审查模式允许路径内的 UTF-8 文本文件。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "当前审查模式允许的文件路径；完全访问模式也支持绝对路径和工作区外路径",
                },
                "content": {"type": "string", "description": "要写入的完整文本"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )

    def __init__(self, context: ToolExecutionContext) -> None:
        self.context = context
        self.resolver = context.paths
        self.review_mode = context.review_mode

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        path = require_string(values, "path", self.name)
        content = values.get("content")
        if not isinstance(content, str):
            raise ValidationError("工具 write 参数校验失败：content 必须是字符串")
        return {"path": path, "content": content}

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self._resolve(arguments)

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        if not self.context.file_mutation_requires_approval:
            return None
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
        atomic_write(resolved.path, data, str(self._safe_path(resolved.relative_path)))
        action = "覆盖" if resolved.exists else "创建"
        return ToolOutput(
            content=f"已{action}文件：{self._safe_path(resolved.relative_path)}",
            metadata={
                "path": self._safe_path(resolved.relative_path),
                "bytes": len(data),
                "action": action,
            },
        )

    def display_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        content = values.get("content")
        content_bytes = len(content.encode("utf-8")) if isinstance(content, str) else "?"
        digest = content_sha256(content) if isinstance(content, str) else "?"
        return (
            f"write 参数摘要：路径={self._safe_path(values.get('path'))}，"
            f"字节数={content_bytes}，内容 sha256={digest}"
        )

    def audit_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        content = values.get("content")
        projection = {
            "path": self._safe_path(values.get("path")),
            "content_bytes": len(content.encode("utf-8")) if isinstance(content, str) else None,
            "content_sha256": content_sha256(content) if isinstance(content, str) else None,
        }
        return f"write 参数摘要：指纹={action_fingerprint(projection)}，投影={projection}"

    def model_metadata(self, output: ToolOutput) -> dict[str, Any]:
        return {
            key: output.metadata[key]
            for key in ("path", "bytes", "action", "cancelled")
            if key in output.metadata
        }

    def audit_summary(self, output: ToolOutput) -> str:
        metadata = output.metadata
        return (
            f"write {output.effective_status().label}：路径={self._safe_path(metadata.get('path'))}，"
            f"动作={metadata.get('action', '[未知]')}，字节数={metadata.get('bytes', 0)}"
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

    @staticmethod
    def _safe_path(path: object) -> object:
        if WorkspacePathResolver._is_sensitive_path(path):
            return "[敏感路径]"
        return path
