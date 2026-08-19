"""工具注册表：维护显式注册工具，只负责名称查找和向模型暴露工具定义。"""

from __future__ import annotations

from collections.abc import Iterable

from ..errors import ConfigError
from ..safety.review_mode import ReviewMode, parse_review_mode
from .base import Tool


class ToolRegistry:
    """显式工具名称到实现的只读注册表。"""

    def __init__(
        self,
        tools: Iterable[Tool],
        model_message_budget: int | None = None,
        review_mode: ReviewMode = ReviewMode.STRICT,
    ) -> None:
        tool_list = tuple(tools)
        names = [tool.name for tool in tool_list]
        self.review_mode = parse_review_mode(review_mode)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigError(f"工具注册失败：工具名称重复：{duplicates}")
        mismatches = sorted(
            {
                f"{tool.name!r}!={getattr(tool.spec, 'name', None)!r}"
                for tool in tool_list
                if getattr(tool, "spec", None) is None
                or tool.name != getattr(tool.spec, "name", None)
            }
        )
        if mismatches:
            raise ConfigError(f"工具注册失败：工具 name/spec 不一致：{mismatches}")
        mode_mismatches = sorted(
            {
                f"{tool.name!r}={getattr(tool, 'review_mode', None)!r}"
                for tool in tool_list
                if getattr(tool, "review_mode", None) is not None
                and parse_review_mode(tool.review_mode) is not self.review_mode
            }
        )
        context_mismatches = sorted(
            {
                f"{tool.name!r}={tool.context.review_mode.value!r}"
                for tool in tool_list
                if getattr(tool, "context", None) is not None
                and tool.context.review_mode is not self.review_mode
            }
        )
        mode_mismatches.extend(context_mismatches)
        if mode_mismatches:
            raise ConfigError(
                "工具注册失败：工具审查模式与 Registry 不一致："
                f"Registry={self.review_mode.value}，工具={mode_mismatches}"
            )
        self._tools = {tool.name: tool for tool in tool_list}
        self._model_message_budget = model_message_budget

    def get(self, name: str) -> Tool | None:
        """按名称查找工具；未知名称由 ToolExecutor 转换为错误结果并审计。"""

        return self._tools.get(name)

    def specs(self):
        """按显式注册顺序返回本次任务的工具定义。"""

        return tuple(tool.spec for tool in self._tools.values())

    def validate_runtime(self) -> None:
        """在 CLI 启动阶段验证 Bash 运行时，避免任务开始后才发现 WSL 或缺少 Git。"""

        for tool in self._tools.values():
            tool.validate_runtime()

    def model_message_budget(self) -> int | None:
        """返回所有工具最终模型消息的统一字节上限。"""

        return self._model_message_budget
