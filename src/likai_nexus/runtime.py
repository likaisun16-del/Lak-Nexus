"""运行时组装层：集中连接配置、模型、工具执行器和 SQLite，避免 CLI 跨层组装依赖。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .executor.registry import ToolRegistry
from .executor.service import ToolExecutor
from .models.base import ModelBackend
from .models.openai_backend import OpenAICompatibleBackend
from .orchestrator.agent_loop import AgentLoop
from .safety.approval import ApprovalHandler, CliApprovalHandler
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
) -> Runtime:
    """完成一次性任务所需的依赖组装，测试可注入 Fake Backend 和静态审批器。"""

    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    tasks.recover_running()
    audit = AuditRepository(database)
    executor = ToolExecutor(
        ToolRegistry.create(settings), approvals or CliApprovalHandler(), audit
    )
    agent = AgentLoop(
        backend or OpenAICompatibleBackend(settings), executor, tasks, settings.max_turns
    )
    return Runtime(agent=agent, tasks=tasks)
