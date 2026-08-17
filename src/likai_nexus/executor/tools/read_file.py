"""read 工具：在工作区内分页读取 UTF-8 文本，并限制行数和返回字节数。"""

from __future__ import annotations

import asyncio
from typing import Any

from ...errors import ToolExecutionError, ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.paths import WorkspacePathResolver
from ...safety.redaction import truncate_text
from ..base import ToolOutput
from .common import require_arguments, require_string


class ReadFileTool:
    """只读文件工具，路径限制由共享 WorkspacePathResolver 完成。"""

    name = "read"
    spec = ToolSpec(
        name=name,
        description="读取工作区内的 UTF-8 文本文件，支持按行分页。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区路径"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "default": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, resolver: WorkspacePathResolver, max_lines: int, max_bytes: int) -> None:
        self.resolver = resolver
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        require_string(values, "path", self.name)
        offset = values.get("offset", 0)
        limit = values.get("limit", self.max_lines)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("工具 read 参数校验失败：offset 必须是大于等于 0 的整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError("工具 read 参数校验失败：limit 必须是大于 0 的整数")
        if limit > self.max_lines:
            raise ValidationError(
                f"工具 read 参数校验失败：limit={limit} 超过单次上限 {self.max_lines}"
            )
        return {"path": values["path"], "offset": offset, "limit": limit}

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self.resolver.resolve(arguments["path"], require_exists=True, file_only=True)

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        return None

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        resolved = self.resolver.resolve(arguments["path"], require_exists=True, file_only=True)
        try:
            content, next_offset, truncated, bytes_read = self._read(
                resolved.path, arguments["offset"], arguments["limit"]
            )
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                f"读取文件失败：{resolved.relative_path} 不是有效 UTF-8 文本，无法按文本读取"
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(
                f"读取文件失败：目标 {resolved.relative_path}，原因：{type(exc).__name__}: {exc}"
            ) from exc
        if truncated:
            content += f"\n[内容已截断：请使用 offset={next_offset} 继续读取]"
        content, _ = truncate_text(content, self.max_bytes)
        return ToolOutput(
            content=content,
            metadata={
                "path": resolved.relative_path,
                "offset": arguments["offset"],
                "next_offset": next_offset,
                "truncated": truncated,
                "bytes": bytes_read,
            },
        )

    def _read(self, path, offset: int, limit: int) -> tuple[str, int, bool, int]:
        lines: list[str] = []
        bytes_read = 0
        current_offset = offset
        truncated = False
        with path.open("r", encoding="utf-8", newline="") as file:
            for _ in range(offset):
                if file.readline() == "":
                    return "", offset, False, 0
            while len(lines) < limit:
                line = file.readline()
                if line == "":
                    break
                encoded = line.encode("utf-8")
                remaining = self.max_bytes - bytes_read
                if len(encoded) > remaining:
                    if remaining > 0:
                        lines.append(encoded[:remaining].decode("utf-8", errors="ignore"))
                        bytes_read += len(lines[-1].encode("utf-8"))
                    truncated = True
                    break
                lines.append(line)
                bytes_read += len(encoded)
                current_offset += 1
            if not truncated and len(lines) >= limit and file.readline() != "":
                truncated = True
        return "".join(lines), current_offset, truncated, bytes_read
