"""运行时组装层：集中连接配置、模型、工具执行器和 SQLite，避免 CLI 跨层组装依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .errors import ConfigError, ToolExecutionError
from .executor.registry import ToolRegistry
from .executor.service import ToolExecutor
from .executor.tools import build_builtin_tools
from .models.base import ModelBackend
from .models.openai_backend import OpenAICompatibleBackend
from .orchestrator.agent_loop import AgentLoop
from .orchestrator.events import EventSink
from .safety.approval import ApprovalHandler, CliApprovalHandler
from .safety.review_mode import ReviewMode, parse_review_mode
from .storage.audit_repository import AuditRepository
from .storage.database import Database
from .storage.task_repository import TaskRepository


@dataclass(frozen=True, slots=True)
class Runtime:
    """CLI 需要的最小运行时对象，不暴露具体工具实现细节。"""

    agent: AgentLoop
    tasks: TaskRepository


def build_runtime(
    settings: Settings,
    *,
    backend: ModelBackend | None = None,
    approvals: ApprovalHandler | None = None,
    review_mode: ReviewMode = ReviewMode.STRICT,
    event_sink: EventSink | None = None,
) -> Runtime:
    """完成一次性任务所需的依赖组装，测试可注入 Fake Backend 和静态审批器。"""

    mode = parse_review_mode(review_mode)
    approval_handler = approvals or CliApprovalHandler()
    registry = ToolRegistry(build_builtin_tools(settings, mode), settings.max_output_bytes, mode)
    if mode is not ReviewMode.FULL_ACCESS:
        try:
            registry.validate_runtime()
        except ToolExecutionError as exc:
            raise ConfigError(f"运行时配置失败：{exc}") from exc
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    tasks.recover_running()
    audit = AuditRepository(database)
    executor = ToolExecutor(
        registry, approval_handler, audit, mode, event_sink
    )
    agent = AgentLoop(
        backend or OpenAICompatibleBackend(settings),
        executor,
        tasks,
        settings.max_turns,
        review_mode=mode,
        approvals=approval_handler,
        event_sink=event_sink,
    )
    return Runtime(agent=agent, tasks=tasks)
