"""工具执行上下文：集中提供审查模式、路径能力和命令限制，避免 Tool 各自解释模式。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..safety.command_policy import CommandPolicy
from ..safety.paths import WorkspacePathResolver
from ..safety.review_mode import ReviewMode, parse_review_mode


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """一次运行时共享的工具能力上下文。"""

    settings: Settings
    review_mode: ReviewMode
    paths: WorkspacePathResolver
    commands: CommandPolicy

    @classmethod
    def from_settings(
        cls, settings: Settings, review_mode: ReviewMode | str
    ) -> ToolExecutionContext:
        """按审查模式创建唯一的路径和命令能力边界。"""

        mode = parse_review_mode(review_mode)
        full_access = mode is ReviewMode.FULL_ACCESS
        paths = WorkspacePathResolver(
            settings.workspace_root,
            review_mode=mode,
            allow_external=full_access,
            allow_sensitive=full_access,
            enforce_symlink_safety=not full_access,
            protected_paths=() if full_access else (settings.data_root,),
        )
        return cls(settings, mode, paths, CommandPolicy(paths, mode))

    @property
    def file_mutation_requires_approval(self) -> bool:
        """表示文件写入或修改是否需要逐次人工审批。"""

        return self.review_mode is ReviewMode.STRICT

    @property
    def shell_requires_approval(self) -> bool:
        """表示 Shell 调用是否需要逐次人工审批。"""

        return self.review_mode is not ReviewMode.FULL_ACCESS

    @property
    def shell_uses_restricted_argv(self) -> bool:
        """表示 Shell 是否必须执行命令策略生成的固定 argv。"""

        return self.review_mode is ReviewMode.STRICT
