"""单元测试共享夹具：为工具、事件和审查模式测试提供可隔离的扩展工具。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from likai_nexus.config import Settings
from likai_nexus.events import RuntimeEvent
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository
from likai_nexus.tools.base import Tool, ToolOutput
from likai_nexus.tools.builtin import build_builtin_tools
from likai_nexus.tools.builtin.bash import BashTool
from likai_nexus.tools.contracts import ToolSpec
from likai_nexus.tools.registry import ToolRegistry


def run(coro):
    """在无 pytest-asyncio 依赖时运行异步测试。"""

    return asyncio.run(coro)


class EchoTool(Tool):
    """测试用扩展工具，验证核心层不需要新增同名分支。"""

    name = "echo"
    spec = ToolSpec(
        name=name,
        description="返回固定文本的测试工具。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    def validate(self, arguments: object) -> dict:
        if arguments != {}:
            raise ValueError("echo 测试工具只接受空参数")
        return {}

    def check_safety(self, arguments: dict) -> None:
        return

    def approval_request(self, arguments: dict):
        return None

    async def execute(self, arguments: dict, cancel_event=None) -> ToolOutput:
        return ToolOutput("echo 完成", metadata={"secret": "TEST_SECRET"})


class CancelledTool(Tool):
    """测试工具，验证执行器会把协程取消转换成明确的终态事件和审计状态。"""

    name = "cancelled"
    spec = ToolSpec(
        name=name,
        description="测试取消事件。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    def validate(self, arguments: object) -> dict:
        return {}

    def check_safety(self, arguments: dict) -> None:
        return

    def approval_request(self, arguments: dict):
        return None

    async def execute(self, arguments: dict, cancel_event=None) -> ToolOutput:
        raise asyncio.CancelledError


class MetadataTool(EchoTool):
    """测试工具，声明自定义状态字段以验证核心层不维护工具白名单。"""

    name = "metadata"
    spec = ToolSpec(
        name=name,
        description="返回自定义游标的测试工具。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    def model_metadata(self, output: ToolOutput) -> dict:
        return {"custom_cursor": "0:1", "custom_complete": True}


class DisplayProjectionTool(Tool):
    """测试第三方展示投影异常不会改变已经成功的工具结果。"""

    name = "display_projection"
    spec = ToolSpec(
        name=name,
        description="返回成功结果并模拟展示投影故障。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.display_calls = 0
        self.execute_calls = 0

    def validate(self, arguments: object) -> dict:
        return {}

    def check_safety(self, arguments: dict) -> None:
        return

    def approval_request(self, arguments: dict):
        return None

    async def execute(self, arguments: dict, cancel_event=None) -> ToolOutput:
        self.execute_calls += 1
        return ToolOutput("工具成功正文")

    def display_arguments(self, arguments: object) -> str:
        if self.mode == "argument-raise":
            raise RuntimeError("指令展示故障")
        if self.mode == "argument-weird":
            return ExplodingString()
        if self.mode == "argument-deep":
            value: dict = {}
            cursor = value
            for _ in range(1500):
                nested: dict = {}
                cursor["nested"] = nested
                cursor = nested
            return value
        return super().display_arguments(arguments)

    def display_result(self, output: ToolOutput | None) -> dict:
        self.display_calls += 1
        if self.mode == "raise":
            raise RuntimeError("展示投影故障")
        if self.mode == "weird":
            return {"stdout": ExplodingString()}
        if self.mode == "deep":
            value: dict = {"stdout": "工具成功正文"}
            cursor = value
            for _ in range(25):
                nested: dict = {}
                cursor["nested"] = nested
                cursor = nested
            return value
        cycle: dict = {}
        cycle["self"] = cycle
        return cycle


class ExplodingString:
    """测试展示投影返回字符串形态对象时不会执行其不安全的转换。"""

    def __str__(self) -> str:
        raise RuntimeError("异常字符串对象")


class MismatchedTool(EchoTool):
    """测试工具，故意制造 name/spec 不一致。"""

    name = "mismatched"


class RecordingSink:
    """记录结构化事件，便于验证展示故障隔离和事件顺序。"""

    def __init__(self, fail: bool = False) -> None:
        self.events: list[RuntimeEvent] = []
        self.fail = fail

    def emit(self, event: RuntimeEvent) -> None:
        if self.fail:
            raise RuntimeError("展示端故障")
        self.events.append(event)


def build_components(tmp_path: Path, mode: ReviewMode, approvals: StaticApprovalHandler):
    """组装指定审查模式的隔离任务、审计和工具执行依赖。"""

    settings = Settings(
        workspace_root=tmp_path,
        project_root=tmp_path,
        database_path=tmp_path.parent / f"{tmp_path.name}-{mode.value}.sqlite3",
        bash_path=Path(BashTool.discover_bash_path())
        if BashTool.discover_bash_path()
        else None,
        max_output_bytes=256,
        max_read_lines=20,
        max_read_bytes=256,
        default_bash_timeout_seconds=10,
        max_bash_timeout_seconds=20,
        max_turns=5,
    )
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    registry = ToolRegistry(build_builtin_tools(settings, mode), settings.max_output_bytes, mode)
    executor = ToolExecutor(registry, approvals, audit, mode)
    return settings, tasks, audit, executor
