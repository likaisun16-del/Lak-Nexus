"""工具注册表：固定注册四个工具，只负责名称查找和向模型暴露工具定义。"""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Settings
from ..safety.command_policy import CommandPolicy
from ..safety.paths import WorkspacePathResolver
from .base import Tool
from .tools.bash import BashTool
from .tools.edit_file import EditFileTool
from .tools.read_file import ReadFileTool
from .tools.write_file import WriteFileTool


class ToolRegistry:
    """四工具名称到实现的只读注册表。"""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @classmethod
    def create(cls, settings: Settings) -> ToolRegistry:
        resolver = WorkspacePathResolver(settings.workspace_root)
        policy = CommandPolicy()
        return cls(
            (
                ReadFileTool(resolver, settings.max_read_lines, settings.max_read_bytes),
                WriteFileTool(resolver),
                EditFileTool(resolver, settings.max_output_bytes),
                BashTool(settings, policy),
            )
        )

    def get(self, name: str) -> Tool | None:
        """按名称查找工具；未知名称由 ToolExecutor 转换为错误结果并审计。"""

        return self._tools.get(name)

    def specs(self):
        """以稳定顺序返回四个工具定义。"""

        return tuple(self._tools[name].spec for name in ("read", "write", "edit", "bash"))
