"""工具一级扩展域：承载工具契约、执行上下文、目录和内置工具实现。"""

from .base import Tool, ToolOutput, safe_argument_summary
from .context import ToolExecutionContext
from .contracts import (
    ToolAuditProjection,
    ToolCall,
    ToolDisplayField,
    ToolDisplayProjection,
    ToolModelProjection,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from .registry import ToolRegistry

__all__ = [
    "Tool",
    "ToolAuditProjection",
    "ToolCall",
    "ToolDisplayField",
    "ToolDisplayProjection",
    "ToolExecutionContext",
    "ToolModelProjection",
    "ToolOutput",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolStatus",
    "safe_argument_summary",
]
