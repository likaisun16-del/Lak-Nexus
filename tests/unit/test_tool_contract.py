"""工具公共契约测试：覆盖注册、终态、投影隔离和审计脱敏。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_support import RecordingSink

from likai_nexus.config import Settings
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository
from likai_nexus.tools.base import Tool, ToolOutput
from likai_nexus.tools.contracts import (
    ToolCall,
    ToolDisplayField,
    ToolDisplayProjection,
    ToolSpec,
    ToolStatus,
)
from likai_nexus.tools.registry import ToolRegistry


def run(coro):
    """在无 pytest-asyncio 依赖时运行异步契约测试。"""

    return asyncio.run(coro)


class ContractTool(Tool):
    """最小扩展工具，用于验证 ToolExecutor 不依赖具体内置工具名称。"""

    name = "contract"
    spec = ToolSpec(
        name=name,
        description="测试结构化工具契约。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    def __init__(self, *, status: ToolStatus = ToolStatus.SUCCESS, display_error: bool = False):
        self.result_status = status
        self.display_error = display_error

    def validate(self, arguments: object) -> dict:
        if arguments != {}:
            raise ValueError("contract 测试工具只接受空参数")
        return {}

    def check_safety(self, arguments: dict) -> None:
        return

    def approval_request(self, arguments: dict):
        return None

    async def execute(self, arguments: dict, cancel_event=None) -> ToolOutput:
        return ToolOutput(
            "OPENAI_API_KEY=MODEL_SECRET",
            metadata={"next_cursor": "0:1", "truncated": False},
            status=self.result_status,
        )

    def display_result(self, output: ToolOutput | None) -> ToolDisplayProjection:
        if self.display_error:
            raise RuntimeError("测试展示投影故障")
        return ToolDisplayProjection((ToolDisplayField("状态", output.effective_status().value),))

    def model_metadata(self, output: ToolOutput) -> dict:
        return {"next_cursor": output.metadata["next_cursor"], "truncated": output.metadata["truncated"]}

    def audit_arguments(self, arguments: object) -> str:
        return "contract 参数摘要：api_key=TOP_SECRET"


class RaisesTool(ContractTool):
    """测试未预期异常必须转换为失败终态并完成审计。"""

    name = "raises"
    spec = ToolSpec(
        name=name,
        description="抛出测试异常的工具。",
        input_schema={"type": "object", "additionalProperties": False},
    )

    async def execute(self, arguments: dict, cancel_event=None) -> ToolOutput:
        raise RuntimeError("测试工具执行异常")


def executor_for(tmp_path: Path, tool: Tool) -> tuple[ToolExecutor, TaskRepository, AuditRepository]:
    """构造只包含一个扩展工具的执行器，复用生产注册和审计路径。"""

    settings = Settings(
        workspace_root=tmp_path,
        project_root=tmp_path,
        database_path=tmp_path.parent / "tool-contract.sqlite3",
        max_output_bytes=256,
    )
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    registry = ToolRegistry((tool,), settings.max_output_bytes, ReviewMode.STRICT)
    return ToolExecutor(registry, StaticApprovalHandler(True), audit), tasks, audit


def test_registry_exposes_explicit_name_and_spec_contract(tmp_path: Path) -> None:
    executor, _, _ = executor_for(tmp_path, ContractTool())

    assert executor.registry.get("contract") is not None
    assert executor.registry.specs()[0].name == "contract"
    assert executor.registry.specs()[0].input_schema["type"] == "object"


def test_structured_status_is_preserved_in_tool_result(tmp_path: Path) -> None:
    executor, tasks, _ = executor_for(tmp_path, ContractTool(status=ToolStatus.TIMEOUT))
    tasks.create("status-task", "status")

    result = run(executor.execute("status-task", ToolCall("status-call", "contract", {})))

    assert result.status is ToolStatus.TIMEOUT
    assert result.is_error
    assert result.metadata["next_cursor"] == "0:1"


def test_explicit_terminal_status_is_consistent_across_event_result_and_audit(
    tmp_path: Path,
) -> None:
    executor, tasks, audit = executor_for(tmp_path, ContractTool(status=ToolStatus.TIMEOUT))
    sink = RecordingSink()
    executor.event_sink = sink
    tasks.create("timeout-task", "timeout")

    result = run(executor.execute("timeout-task", ToolCall("timeout-call", "contract", {})))

    row = audit.list_tool_calls("timeout-task")[0]
    event = next(event for event in sink.events if event.event_type == "tool_timed_out")
    assert result.status is ToolStatus.TIMEOUT
    assert result.audit is not None
    assert "超时" in result.audit.result_summary
    assert "成功" not in result.audit.result_summary
    assert row["status"] == ToolStatus.TIMEOUT.value
    assert "超时" in row["result_summary"]
    assert "成功" not in row["result_summary"]
    assert event.metadata["status"] == ToolStatus.TIMEOUT.value


def test_explicit_cancelled_status_is_consistent_across_event_result_and_audit(
    tmp_path: Path,
) -> None:
    executor, tasks, audit = executor_for(tmp_path, ContractTool(status=ToolStatus.CANCELLED))
    sink = RecordingSink()
    executor.event_sink = sink
    tasks.create("cancel-task", "cancel")

    result = run(executor.execute("cancel-task", ToolCall("cancel-call", "contract", {})))

    row = audit.list_tool_calls("cancel-task")[0]
    event = next(event for event in sink.events if event.event_type == "tool_cancelled")
    assert result.status is ToolStatus.CANCELLED
    assert result.audit is not None
    assert "取消" in result.audit.result_summary
    assert row["status"] == ToolStatus.CANCELLED.value
    assert event.metadata["status"] == ToolStatus.CANCELLED.value


def test_tool_exception_returns_failed_result_and_failed_audit(tmp_path: Path) -> None:
    executor, tasks, audit = executor_for(tmp_path, RaisesTool())
    tasks.create("exception-task", "exception")

    result = run(executor.execute("exception-task", ToolCall("exception-call", "raises", {})))

    row = audit.list_tool_calls("exception-task")[0]
    assert result.status is ToolStatus.FAILED
    assert result.is_error
    assert row["status"] == ToolStatus.FAILED.value
    assert "RuntimeError" in row["result_summary"]


def test_display_projection_failure_is_isolated_from_success(tmp_path: Path) -> None:
    executor, tasks, _ = executor_for(tmp_path, ContractTool(display_error=True))
    tasks.create("display-task", "display")

    result = run(executor.execute("display-task", ToolCall("display-call", "contract", {})))

    assert result.status is ToolStatus.SUCCESS
    assert result.display.fields == ()
    assert "MODEL_SECRET" not in result.content


def test_model_projection_keeps_status_and_redacts_sensitive_content(tmp_path: Path) -> None:
    executor, tasks, _ = executor_for(tmp_path, ContractTool())
    tasks.create("model-task", "model")

    result = run(executor.execute("model-task", ToolCall("model-call", "contract", {})))

    assert "MODEL_SECRET" not in result.content
    assert result.metadata == {"next_cursor": "0:1", "truncated": False}
    assert "next_cursor" in result.content


def test_audit_projection_redacts_custom_argument_summary(tmp_path: Path) -> None:
    executor, tasks, audit = executor_for(tmp_path, ContractTool())
    tasks.create("audit-task", "audit")

    result = run(executor.execute("audit-task", ToolCall("audit-call", "contract", {})))

    assert result.audit is not None
    assert "TOP_SECRET" not in result.audit.arguments_summary
    assert "TOP_SECRET" not in str(audit.list_tool_calls("audit-task"))
