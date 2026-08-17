"""read 工具：由 ToolExecutor 通过 WorkspacePathResolver 分页读取 UTF-8 文本并限制返回字节数。"""

from __future__ import annotations

import asyncio
from typing import Any

from ...errors import ToolExecutionError, ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.paths import WorkspacePathResolver
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
                "byte_offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "default": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, resolver: WorkspacePathResolver, max_lines: int, max_bytes: int) -> None:
        if max_bytes < 4:
            raise ValueError("工具 read 配置错误：max_bytes 至少为 4，才能保证截断游标可推进")
        self.resolver = resolver
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        require_string(values, "path", self.name)
        offset = values.get("offset", 0)
        byte_offset = values.get("byte_offset", 0)
        limit = values.get("limit", self.max_lines)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("工具 read 参数校验失败：offset 必须是大于等于 0 的整数")
        if isinstance(byte_offset, bool) or not isinstance(byte_offset, int) or byte_offset < 0:
            raise ValidationError("工具 read 参数校验失败：byte_offset 必须是大于等于 0 的整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError("工具 read 参数校验失败：limit 必须是大于 0 的整数")
        if limit > self.max_lines:
            raise ValidationError(
                f"工具 read 参数校验失败：limit={limit} 超过单次上限 {self.max_lines}"
            )
        return {
            "path": values["path"],
            "offset": offset,
            "byte_offset": byte_offset,
            "limit": limit,
        }

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self.resolver.resolve(arguments["path"], require_exists=True, file_only=True)

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        return None

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        resolved = self.resolver.resolve(arguments["path"], require_exists=True, file_only=True)
        try:
            content, next_offset, next_byte_offset, truncated, bytes_read = self._read(
                resolved.path,
                arguments["offset"],
                arguments["byte_offset"],
                arguments["limit"],
            )
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                f"读取文件失败：{resolved.relative_path} 不是有效 UTF-8 文本，无法按文本读取"
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(
                f"读取文件失败：目标 {resolved.relative_path}，原因：{type(exc).__name__}: {exc}"
            ) from exc
        return ToolOutput(
            content=content,
            metadata={
                "path": resolved.relative_path,
                "offset": arguments["offset"],
                "byte_offset": arguments["byte_offset"],
                "next_offset": next_offset,
                "next_byte_offset": next_byte_offset,
                "next_cursor": f"{next_offset}:{next_byte_offset}",
                "truncated": truncated,
                "bytes": bytes_read,
            },
        )

    def _read(
        self, path, offset: int, byte_offset: int, limit: int
    ) -> tuple[str, int, int, bool, int]:
        chunks: list[bytes] = []
        bytes_read = 0
        next_offset = offset
        next_byte_offset = byte_offset
        truncated = False
        lines_read = 0
        with path.open("rb") as file:
            for line_number, raw_line in enumerate(file):
                if line_number < offset:
                    continue
                # 先验证整行，避免预算不足时因只截取空前缀而漏报二进制内容。
                raw_line.decode("utf-8")
                start = byte_offset if line_number == offset else 0
                if start > len(raw_line):
                    raise ValidationError(
                        f"工具 read 游标无效：offset={offset} 的 byte_offset={byte_offset} 超过行长度"
                    )
                if start == len(raw_line):
                    next_offset = line_number + 1
                    next_byte_offset = 0
                    continue
                if start:
                    try:
                        raw_line[:start].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValidationError(
                            f"工具 read 游标无效：offset={offset}, byte_offset={byte_offset} 不在 UTF-8 字符边界"
                        ) from exc
                raw_line = raw_line[start:]
                if lines_read >= limit:
                    truncated = True
                    next_offset = line_number
                    next_byte_offset = 0
                    break
                remaining = self.max_bytes - bytes_read
                if remaining <= 0:
                    truncated = True
                    next_offset = line_number
                    next_byte_offset = start
                    break
                prefix, was_cut = self._safe_utf8_prefix(raw_line, remaining)
                if not prefix:
                    truncated = True
                    next_offset = line_number
                    next_byte_offset = start
                    break
                chunks.append(prefix)
                bytes_read += len(prefix)
                if was_cut:
                    truncated = True
                    next_offset = line_number
                    next_byte_offset = start + len(prefix)
                    break
                lines_read += 1
                next_offset = line_number + 1
                next_byte_offset = 0
                if lines_read >= limit:
                    if file.readline():
                        truncated = True
                    break
        return (
            b"".join(chunks).decode("utf-8"),
            next_offset,
            next_byte_offset,
            truncated,
            bytes_read,
        )

    @staticmethod
    def _safe_utf8_prefix(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
        """截取完整 UTF-8 前缀，并保证超长单行的游标始终前进。"""

        if len(data) <= max_bytes:
            return data, False
        candidate = data[:max_bytes]
        while candidate:
            try:
                candidate.decode("utf-8")
                return candidate, True
            except UnicodeDecodeError:
                candidate = candidate[:-1]
        return b"", True
