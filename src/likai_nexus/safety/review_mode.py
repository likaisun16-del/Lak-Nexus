"""审查模式定义：供 CLI、运行时、安全策略和任务持久化共同使用。"""

from __future__ import annotations

from enum import StrEnum


class ReviewMode(StrEnum):
    """一次任务固定使用的权限审查模式。"""

    STRICT = "strict"
    RELAXED = "relaxed"
    FULL_ACCESS = "full-access"


def parse_review_mode(value: object) -> ReviewMode:
    """把 CLI、测试或仓储输入转换为明确的审查模式。"""

    if isinstance(value, ReviewMode):
        return value
    try:
        return ReviewMode(str(value))
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ReviewMode)
        raise ValueError(f"审查模式无效：{value!r}，允许值为：{allowed}") from exc
