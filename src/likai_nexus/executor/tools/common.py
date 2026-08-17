"""工具公共辅助：统一参数对象检查和同目录原子写入，避免文件工具复制实现。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...errors import ToolExecutionError, ValidationError


def require_arguments(arguments: object, tool_name: str) -> dict[str, Any]:
    """确保模型传入 JSON 对象，并把具体工具名写入错误点。"""

    if not isinstance(arguments, Mapping):
        raise ValidationError(f"工具 {tool_name} 参数校验失败：arguments 必须是 JSON 对象")
    return dict(arguments)


def require_string(arguments: dict[str, Any], key: str, tool_name: str) -> str:
    """读取非空字符串字段并报告字段级错误。"""

    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"工具 {tool_name} 参数校验失败：{key} 必须是非空字符串")
    return value


def atomic_write(path: Path, data: bytes, relative_path: str) -> None:
    """通过同目录临时文件和 os.replace 原子替换目标，失败时清理临时文件。"""

    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
        raise ToolExecutionError(
            f"写入文件失败：目标 {relative_path}，原因：{type(exc).__name__}: {exc}"
        ) from exc
