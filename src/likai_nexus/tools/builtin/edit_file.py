"""edit 工具：由 ToolExecutor 审批后在 WorkspacePathResolver 范围内替换 UTF-8 文件并返回受限 diff。"""

from __future__ import annotations

import asyncio
import difflib
from typing import Any

from ...errors import ToolExecutionError, ValidationError
from ...safety.approval import ApprovalRequest
from ...safety.paths import ResolvedPath, WorkspacePathResolver
from ...safety.redaction import action_fingerprint, content_sha256, redact_text, truncate_text
from ..base import Tool, ToolOutput
from ..context import ToolExecutionContext
from ..contracts import ToolSpec
from .common import atomic_write, require_arguments, require_string


class EditFileTool(Tool):
    """单块精确替换工具，零次或多次匹配都不会写入文件。"""

    name = "edit"
    spec = ToolSpec(
        name=name,
        description="将允许访问文件中的唯一 old_text 精确替换为 new_text。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "当前审查模式允许的文件路径；完全访问模式也支持绝对路径和工作区外路径",
                },
                "old_text": {"type": "string", "description": "必须唯一出现的原文本"},
                "new_text": {"type": "string", "description": "替换后的文本"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, context: ToolExecutionContext, diff_limit_bytes: int = 64 * 1024
    ) -> None:
        self.context = context
        self.resolver = context.paths
        self.diff_limit_bytes = diff_limit_bytes
        self.review_mode = context.review_mode

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        path = require_string(values, "path", self.name)
        old_text = values.get("old_text")
        new_text = values.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ValidationError("工具 edit 参数校验失败：old_text 必须是非空字符串")
        if not isinstance(new_text, str):
            raise ValidationError("工具 edit 参数校验失败：new_text 必须是字符串")
        return {"path": path, "old_text": old_text, "new_text": new_text}

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self._resolve(arguments)

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        if not self.context.file_mutation_requires_approval:
            return None
        resolved = self._resolve(arguments)
        try:
            text, _ = self._read_text(resolved)
            normalized_old = self._normalize_newlines(arguments["old_text"], text)
            normalized_new = self._normalize_newlines(arguments["new_text"], text)
            matches = text.count(normalized_old)
        except (OSError, UnicodeDecodeError) as exc:
            matches = 0
            reason = f"，预览失败：{type(exc).__name__}"
            source_hash = f"[读取失败:{type(exc).__name__}]"
            diff_preview = "[无法生成修改预览]"
            result_hash = "[不可用]"
        else:
            reason = f"，当前精确匹配次数：{matches}"
            source_hash = content_sha256(text)
            if matches == 1:
                updated = text.replace(normalized_old, normalized_new, 1)
                result_hash = content_sha256(updated)
                diff_preview, diff_truncated = truncate_text(
                    redact_text(self._build_diff(text, updated)), 512
                )
                if diff_truncated:
                    diff_preview += "（diff 已截断）"
            else:
                result_hash = "[不可用]"
                diff_preview = "[当前无法形成唯一修改块]"
        action_data = {
            "path": resolved.relative_path,
            "source_sha256": source_hash,
            "result_sha256": result_hash,
            "old_text_sha256": content_sha256(arguments["old_text"]),
            "new_text_sha256": content_sha256(arguments["new_text"]),
            "matches": matches,
        }
        diff_summary = "diff 摘要：将替换唯一匹配块" if matches == 1 else "diff 摘要：当前无法形成唯一修改块"
        return ApprovalRequest(
            action_type="edit",
            summary=(
                f"修改工作区文件 {resolved.relative_path}，替换文本长度 "
                f"{len(arguments['old_text'])} -> {len(arguments['new_text'])}{reason}，{diff_summary}，"
                f"old sha256={content_sha256(arguments['old_text'])}，"
                f"new sha256={content_sha256(arguments['new_text'])}，预览：{diff_preview}"
            ),
            fingerprint=action_fingerprint(action_data),
            audit_summary=(
                f"修改 {resolved.relative_path}：匹配数={matches}，"
                f"原文件 sha256={source_hash}，结果 sha256={result_hash}，"
                f"old sha256={content_sha256(arguments['old_text'])}，"
                f"new sha256={content_sha256(arguments['new_text'])}"
            ),
        )

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        resolved = self._resolve(arguments)
        try:
            text, bom = self._read_text(resolved)
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                f"修改文件失败：{self._safe_path(resolved.relative_path)} 不是有效 UTF-8 文本"
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(
                f"修改文件失败：无法读取 {self._safe_path(resolved.relative_path)}，"
                f"原因：{type(exc).__name__}: {exc}"
            ) from exc
        old_text = self._normalize_newlines(arguments["old_text"], text)
        new_text = self._normalize_newlines(arguments["new_text"], text)
        matches = text.count(old_text)
        if matches == 0:
            raise ToolExecutionError(
                f"修改文件失败：{self._safe_path(resolved.relative_path)} 未找到匹配文本"
            )
        if matches != 1:
            raise ToolExecutionError(
                f"修改文件失败：{self._safe_path(resolved.relative_path)} 匹配不唯一，"
                f"共找到 {matches} 处"
            )
        if cancel_event and cancel_event.is_set():
            return ToolOutput("修改已取消：收到任务取消信号", is_error=True, metadata={"cancelled": True})
        updated = text.replace(old_text, new_text, 1)
        data = bom + updated.encode("utf-8")
        if resolved.sensitive:
            diff = "[已脱敏]：敏感文件修改 diff 未返回"
        else:
            diff = self._build_diff(text, updated, str(resolved.relative_path))
        atomic_write(resolved.path, data, str(self._safe_path(resolved.relative_path)))
        diff, truncated = truncate_text(diff, self.diff_limit_bytes)
        return ToolOutput(
            content=f"已修改文件：{self._safe_path(resolved.relative_path)}\n{diff}",
            metadata={
                "path": self._safe_path(resolved.relative_path),
                "matches": 1,
                "diff_truncated": truncated,
            },
        )

    def display_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        old_text = values.get("old_text")
        new_text = values.get("new_text")
        return (
            f"edit 参数摘要：路径={self._safe_path(values.get('path'))}，"
            f"old 字节数={self._text_bytes(old_text)}，new 字节数={self._text_bytes(new_text)}，"
            f"old sha256={self._text_hash(old_text)}，new sha256={self._text_hash(new_text)}"
        )

    def audit_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        projection = {
            "path": self._safe_path(values.get("path")),
            "old_text_bytes": self._text_bytes(values.get("old_text")),
            "new_text_bytes": self._text_bytes(values.get("new_text")),
            "old_text_sha256": self._text_hash(values.get("old_text")),
            "new_text_sha256": self._text_hash(values.get("new_text")),
        }
        return f"edit 参数摘要：指纹={action_fingerprint(projection)}，投影={projection}"

    def model_metadata(self, output: ToolOutput) -> dict[str, Any]:
        return {
            key: output.metadata[key]
            for key in ("path", "matches", "diff_truncated", "cancelled")
            if key in output.metadata
        }

    def audit_summary(self, output: ToolOutput) -> str:
        metadata = output.metadata
        return (
            f"edit {output.effective_status().label}：路径={self._safe_path(metadata.get('path'))}，"
            f"匹配数={metadata.get('matches', 0)}，"
            f"diff截断={metadata.get('diff_truncated', False)}"
        )

    def _resolve(self, arguments: dict[str, Any]) -> ResolvedPath:
        return self.resolver.resolve(
            arguments["path"], require_exists=True, file_only=True, reject_symlink=True
        )

    @staticmethod
    def _read_text(resolved: ResolvedPath) -> tuple[str, bytes]:
        data = resolved.path.read_bytes()
        bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
        return data[len(bom) :].decode("utf-8"), bom

    @staticmethod
    def _normalize_newlines(value: str, original: str) -> str:
        newline = "\r\n" if "\r\n" in original else "\n"
        return value.replace("\r\n", "\n").replace("\n", newline)

    @staticmethod
    def _build_diff(original: str, updated: str, relative_path: str = "file") -> str:
        """集中生成审批预览和执行结果使用的受限 diff。"""

        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )

    @staticmethod
    def _text_bytes(value: object) -> int | None:
        return len(value.encode("utf-8")) if isinstance(value, str) else None

    @staticmethod
    def _text_hash(value: object) -> str | None:
        return content_sha256(value) if isinstance(value, str) else None

    @staticmethod
    def _safe_path(path: object) -> object:
        if WorkspacePathResolver._is_sensitive_path(path):
            return "[敏感路径]"
        return path
