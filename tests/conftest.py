"""测试夹具：创建隔离工作区、SQLite 数据库和完整 ToolExecutor 依赖。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from likai_nexus.config import Settings
from likai_nexus.executor.registry import ToolRegistry
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """为每个测试提供独立工作区和数据库，避免污染项目目录。"""

    bash = shutil.which("bash")
    return Settings(
        workspace_root=tmp_path,
        database_path=tmp_path / "audit.sqlite3",
        bash_path=Path(bash) if bash else None,
        max_output_bytes=256,
        max_read_lines=20,
        max_read_bytes=256,
        # Windows 的 WSL bash 启动通常超过 2 秒，测试需覆盖真实可用的 Bash 兼容环境。
        default_bash_timeout_seconds=5,
        max_bash_timeout_seconds=10,
        max_turns=5,
    )


@pytest.fixture
def runtime(settings: Settings):
    """构建测试所需的任务仓储、审计仓储、审批器和工具执行器。"""

    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    approvals = StaticApprovalHandler(True)
    executor = ToolExecutor(ToolRegistry.create(settings), approvals, audit)
    return settings, database, tasks, audit, approvals, executor
