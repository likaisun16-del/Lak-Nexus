"""运行时组装层：集中连接配置、模型、工具执行器和 SQLite，避免 CLI 跨层组装依赖。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .errors import ConfigError, ToolExecutionError
from .events import EventSink
from .executor.service import ToolExecutor
from .models.base import ModelBackend
from .models.openai_backend import OpenAICompatibleBackend
from .orchestrator.agent_loop import AgentLoop
from .safety.approval import ApprovalHandler, CliApprovalHandler
from .safety.review_mode import ReviewMode, parse_review_mode
from .storage.app_data import AppDataManager
from .storage.audit_repository import AuditRepository
from .storage.database import Database
from .storage.task_repository import TaskRepository
from .tools.builtin import build_builtin_tools
from .tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class Runtime:
    """CLI 需要的最小运行时对象，不暴露具体工具实现细节。"""

    agent: AgentLoop
    tasks: TaskRepository


def prepare_runtime(settings: Settings) -> tuple[str, ...]:
    """由运行时启动层准备应用目录和旧数据库，不让配置对象反向初始化存储。"""

    return AppDataManager(
        settings.project_root,
        settings.workspace_root,
        settings.database_path,
        settings.use_default_database,
    ).prepare()


def build_runtime(
    settings: Settings,
    *,
    backend: ModelBackend | None = None,
    approvals: ApprovalHandler | None = None,
    review_mode: ReviewMode = ReviewMode.STRICT,
    event_sink: EventSink | None = None,
    full_access_confirmed: bool = False,
    on_full_access_confirmed: Callable[[], None] | None = None,
) -> Runtime:
    """完成一次性任务所需的依赖组装，测试可注入 Fake Backend 和静态审批器。"""

    mode = parse_review_mode(review_mode)
    prepare_runtime(settings)
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
        full_access_confirmed=full_access_confirmed,
        on_full_access_confirmed=on_full_access_confirmed,
    )
    return Runtime(agent=agent, tasks=tasks)
