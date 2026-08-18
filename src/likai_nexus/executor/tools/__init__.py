"""内置工具注册点：集中组装经过审查的工具，不让核心执行层绑定具体工具名称。"""

from __future__ import annotations

from ...config import READ_STATUS_RESERVE_BYTES, Settings
from ...safety.command_policy import CommandPolicy
from ...safety.paths import WorkspacePathResolver
from ...safety.review_mode import ReviewMode, parse_review_mode
from ..base import Tool
from .bash import BashTool
from .edit_file import EditFileTool
from .read_file import ReadFileTool
from .write_file import WriteFileTool


def build_builtin_tools(settings: Settings, review_mode: ReviewMode | str) -> tuple[Tool, ...]:
    """按审查模式组装内置工具；扩展工具应在此显式注册后交给 ToolRegistry。"""

    mode = parse_review_mode(review_mode)
    full_access = mode is ReviewMode.FULL_ACCESS
    resolver = WorkspacePathResolver(
        settings.workspace_root,
        review_mode=mode,
        allow_external=full_access,
        allow_sensitive=full_access,
        enforce_symlink_safety=not full_access,
    )
    policy = CommandPolicy(resolver, mode)
    # read 必须按最终模型消息预算生成游标，避免 ToolExecutor 二次截断正文后跳过字节。
    read_bytes = min(
        settings.max_read_bytes,
        settings.max_output_bytes - READ_STATUS_RESERVE_BYTES,
    )
    return (
        ReadFileTool(resolver, settings.max_read_lines, read_bytes),
        WriteFileTool(resolver),
        EditFileTool(resolver, settings.max_output_bytes),
        BashTool(settings, policy),
    )
