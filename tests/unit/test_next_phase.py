"""下一阶段能力测试：覆盖动态工具、过程事件、审查模式和 SQLite 兼容迁移。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from likai_nexus.config import Settings
from likai_nexus.errors import ConfigError
from likai_nexus.executor.base import Tool, ToolOutput
from likai_nexus.executor.registry import ToolRegistry
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.executor.tools import build_builtin_tools
from likai_nexus.executor.tools.bash import BashTool
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.events import RuntimeEvent
from likai_nexus.orchestrator.schemas import (
    AssistantTurn,
    TaskStatus,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from likai_nexus.runtime import build_runtime
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.safety.redaction import redact_text
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository


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
        database_path=tmp_path / f"{mode.value}.sqlite3",
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


def test_dynamic_tool_is_discovered_and_audited_without_core_name_branch(tmp_path: Path) -> None:
    database = Database(tmp_path / "dynamic.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    approvals = StaticApprovalHandler(True)
    executor = ToolExecutor(
        ToolRegistry((EchoTool(),), model_message_budget=256), approvals, audit
    )
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("echo-1", "echo", {}),)),
            AssistantTurn("扩展工具完成"),
        ]
    )

    result = run(AgentLoop(backend, executor, tasks).run("调用扩展工具", task_id="dynamic"))

    assert result.status is TaskStatus.SUCCESS
    assert "当前请求提供的工具完成工作：echo" in backend.messages[0][0].content
    assert "审查模式是 strict" in backend.messages[0][0].content
    assert "read" not in backend.messages[0][0].content
    assert audit.list_tool_calls("dynamic")[0]["tool_name"] == "echo"
    assert "TEST_SECRET" not in str(audit.list_tool_calls("dynamic"))


def test_untrusted_tool_call_fields_are_safe_before_audit_persistence(tmp_path: Path) -> None:
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(tmp_path, ReviewMode.STRICT, approvals)
    tasks.create("untrusted-audit", "安全审计字段", ReviewMode.STRICT)

    known = run(
        executor.execute(
            "untrusted-audit",
            ToolCall(
                "token=KNOWN_CALL_SECRET",
                "write",
                {"path": "known.txt", "content": "safe"},
            ),
        )
    )
    known_invalid = run(
        executor.execute(
            "untrusted-audit",
            ToolCall(
                "token=KNOWN_ARGUMENT_CALL_SECRET",
                "write",
                {"token=KNOWN_ARGUMENT_SECRET": "value"},
            ),
        )
    )
    unknown = run(
        executor.execute(
            "untrusted-audit",
            ToolCall(
                "token=UNKNOWN_CALL_SECRET",
                "token=UNKNOWN_NAME_SECRET",
                {"token=UNKNOWN_ARGUMENT_SECRET": "value"},
            ),
        )
    )

    assert not known.is_error
    assert known_invalid.is_error
    assert unknown.is_error
    assert "未知工具" in unknown.content
    rows = audit.list_tool_calls("untrusted-audit")
    approvals_rows = audit.list_approvals("untrusted-audit")
    persisted = str(tasks.get("untrusted-audit")) + str(rows) + str(approvals_rows)
    for sentinel in (
        "KNOWN_CALL_SECRET",
        "KNOWN_ARGUMENT_CALL_SECRET",
        "KNOWN_ARGUMENT_SECRET",
        "UNKNOWN_CALL_SECRET",
        "UNKNOWN_NAME_SECRET",
        "UNKNOWN_ARGUMENT_SECRET",
    ):
        assert sentinel not in persisted
    known_row = next(row for row in rows if row["tool_name"] == "write")
    assert known_row["tool_call_id"] != "token=KNOWN_CALL_SECRET"
    assert approvals_rows[0]["tool_call_id"] != "token=KNOWN_CALL_SECRET"


def test_unlabelled_credential_shaped_tool_fields_are_safe_everywhere(tmp_path: Path) -> None:
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(tmp_path, ReviewMode.STRICT, approvals)
    sink = RecordingSink()
    executor.event_sink = sink
    tasks.create("credential-shaped-audit", "安全审计字段", ReviewMode.STRICT)
    call_id = "sk-proj-AbC123xYz789Qwe"
    tool_name = "ghp_AbCdEf1234567890"
    field_name = "eyJAbCdEf12345678.ghIjKlMnOpQrStUv.WxYz0123456789"

    result = run(
        executor.execute(
            "credential-shaped-audit",
            ToolCall(call_id, tool_name, {field_name: "safe"}),
        )
    )

    assert result.is_error
    assert "未知工具" in result.content
    rows = audit.list_tool_calls("credential-shaped-audit")
    approvals_rows = audit.list_approvals("credential-shaped-audit")
    persisted_and_displayed = (
        str(tasks.get("credential-shaped-audit"))
        + str(rows)
        + str(approvals_rows)
        + result.content
        + str([event.message for event in sink.events])
    )
    for sentinel in (call_id, tool_name, field_name):
        assert sentinel not in persisted_and_displayed
    assert rows[0]["tool_name"].startswith("unknown-tool:")
    assert rows[0]["tool_call_id"].startswith("call:")
    assert rows[0]["arguments_redacted"]


def test_audit_start_failure_does_not_echo_untrusted_tool_fields(tmp_path: Path) -> None:
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(tmp_path, ReviewMode.STRICT, approvals)
    sink = RecordingSink()
    executor.event_sink = sink
    call_id = "sk_live_AbC123xYz789Qwe987654"
    tool_name = "tool_live_AbC123xYz789Qwe987654"

    def fail_start(*args, **kwargs):
        raise OSError("simulated audit storage outage")

    audit.start_tool_call = fail_start
    backend = FakeModelBackend(
        [AssistantTurn("", (ToolCall(call_id, tool_name, {}),))]
    )

    result = run(
        AgentLoop(backend, executor, tasks, event_sink=sink).run(
            "审计启动失败", task_id="audit-start-failure"
        )
    )

    assert result.status is TaskStatus.FAILED
    persisted_and_displayed = (
        str(result)
        + str(tasks.get("audit-start-failure"))
        + str(audit.list_tool_calls("audit-start-failure"))
        + str(audit.list_approvals("audit-start-failure"))
        + str([event.message for event in sink.events])
    )
    assert call_id not in persisted_and_displayed
    assert tool_name not in persisted_and_displayed
    assert any(event.event_type == "task_finished" for event in sink.events)


def test_approval_audit_failure_does_not_echo_untrusted_call_id(tmp_path: Path) -> None:
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(tmp_path, ReviewMode.STRICT, approvals)
    sink = RecordingSink()
    executor.event_sink = sink
    call_id = "sk_live_AbC123xYz789Qwe987654"

    def fail_record(*args, **kwargs):
        raise OSError("simulated approval audit outage")

    audit.record_approval = fail_record
    backend = FakeModelBackend(
        [
            AssistantTurn(
                "",
                (ToolCall(call_id, "write", {"path": "safe.txt", "content": "safe"}),),
            )
        ]
    )

    result = run(
        AgentLoop(backend, executor, tasks, event_sink=sink).run(
            "审批审计失败", task_id="approval-audit-failure"
        )
    )

    assert result.status is TaskStatus.FAILED
    rows = audit.list_tool_calls("approval-audit-failure")
    persisted_and_displayed = (
        str(result)
        + str(tasks.get("approval-audit-failure"))
        + str(rows)
        + str(audit.list_approvals("approval-audit-failure"))
        + str([event.message for event in sink.events])
    )
    assert call_id not in persisted_and_displayed
    assert rows[0]["status"] == "failed"
    assert any(event.event_type == "task_finished" for event in sink.events)


def test_registered_extension_runs_through_builtin_registration_point(settings) -> None:
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    mode = ReviewMode.STRICT
    registered_tools = (*build_builtin_tools(settings, mode), EchoTool())
    executor = ToolExecutor(
        ToolRegistry(registered_tools, settings.max_output_bytes, mode),
        StaticApprovalHandler(True),
        audit,
    )
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("registered-echo", "echo", {}),)),
            AssistantTurn("注册工具完成"),
        ]
    )

    result = run(AgentLoop(backend, executor, tasks).run("调用注册工具", task_id="registered"))

    assert result.status is TaskStatus.SUCCESS
    assert audit.list_tool_calls("registered")[0]["tool_name"] == "echo"
    assert "echo" in {spec.name for spec in executor.registry.specs()}


def test_agent_loop_inherits_executor_review_mode(tmp_path: Path) -> None:
    database = Database(tmp_path / "mode-inheritance.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    registry = ToolRegistry(
        (EchoTool(),), model_message_budget=256, review_mode=ReviewMode.RELAXED
    )
    executor = ToolExecutor(registry, StaticApprovalHandler(True), audit)
    result = run(
        AgentLoop(FakeModelBackend([AssistantTurn("模式继承")]), executor, tasks).run(
            "检查模式继承", task_id="mode-inheritance"
        )
    )

    assert result.status is TaskStatus.SUCCESS
    assert tasks.get("mode-inheritance")["review_mode"] == ReviewMode.RELAXED.value


def test_review_mode_mismatch_is_rejected_at_executor_and_agent_boundaries(tmp_path: Path) -> None:
    database = Database(tmp_path / "mode-mismatch.sqlite3")
    database.initialize()
    audit = AuditRepository(database)
    tasks = TaskRepository(database)

    modes = tuple(ReviewMode)
    for registry_mode in modes:
        for requested_mode in modes:
            if registry_mode is requested_mode:
                continue
            registry = ToolRegistry((EchoTool(),), 256, registry_mode)
            with pytest.raises(ConfigError, match="ToolExecutor.*不一致"):
                ToolExecutor(
                    registry,
                    StaticApprovalHandler(True),
                    audit,
                    requested_mode,
                )

    for executor_mode in modes:
        registry = ToolRegistry((EchoTool(),), 256, executor_mode)
        executor = ToolExecutor(registry, StaticApprovalHandler(True), audit)
        for requested_mode in modes:
            if executor_mode is requested_mode:
                continue
            with pytest.raises(ConfigError, match="AgentLoop.*不一致"):
                AgentLoop(
                    FakeModelBackend([AssistantTurn("不应执行")]),
                    executor,
                    tasks,
                    review_mode=requested_mode,
                )


def test_registry_rejects_name_and_spec_mismatch() -> None:
    with pytest.raises(ConfigError, match="name/spec 不一致"):
        ToolRegistry((MismatchedTool(),))


def test_registry_rejects_builtin_tool_mode_mismatch(settings) -> None:
    full_tools = build_builtin_tools(settings, ReviewMode.FULL_ACCESS)

    with pytest.raises(ConfigError, match="工具审查模式与 Registry 不一致"):
        ToolRegistry(full_tools, settings.max_output_bytes)


def test_custom_tool_metadata_survives_small_model_budget(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    executor = ToolExecutor(
        ToolRegistry((MetadataTool(),), model_message_budget=96),
        StaticApprovalHandler(True),
        audit,
    )
    tasks.create("metadata-task", "调用自定义状态工具", ReviewMode.STRICT)

    result = run(
        executor.execute("metadata-task", ToolCall("metadata-call", "metadata", {}))
    )

    assert not result.is_error
    assert "custom_cursor" in result.content
    assert "custom_complete" in result.content


def test_registry_rejects_duplicate_tool_names() -> None:
    with pytest.raises(ConfigError, match="工具名称重复"):
        ToolRegistry((EchoTool(), EchoTool()))


def test_sensitive_json_and_assignment_values_are_redacted() -> None:
    value = (
        'TOKEN=TEST_SECRET AWS_ACCESS_KEY_ID=AWS_SECRET '
        '{"password": "JSON_SECRET", "access_key": "JSON_ACCESS_SECRET", '
        '"safe": "visible"} '
        "-----BEGIN PRIVATE KEY-----\nPRIVATE_SECRET\n-----END PRIVATE KEY-----"
    )

    redacted = redact_text(value)

    assert "TEST_SECRET" not in redacted
    assert "AWS_SECRET" not in redacted
    assert "JSON_SECRET" not in redacted
    assert "JSON_ACCESS_SECRET" not in redacted
    assert "PRIVATE_SECRET" not in redacted
    assert "visible" in redacted


def test_incomplete_private_key_is_redacted_after_output_truncation() -> None:
    value = "-----BEGIN PRIVATE KEY-----\nPARTIAL_PRIVATE_SECRET"

    redacted = redact_text(value)

    assert "PARTIAL_PRIVATE_SECRET" not in redacted
    assert "[已脱敏：私钥内容]" in redacted


def test_agent_loop_redacts_final_model_message(tmp_path: Path) -> None:
    database = Database(tmp_path / "final-redaction.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    executor = ToolExecutor(
        ToolRegistry((EchoTool(),), model_message_budget=256),
        StaticApprovalHandler(True),
        audit,
    )
    backend = FakeModelBackend([AssistantTurn("OPENAI_API_KEY=MODEL_SECRET")])

    result = run(AgentLoop(backend, executor, tasks).run("输出最终回答", task_id="final-redaction"))

    assert result.status is TaskStatus.SUCCESS
    assert "MODEL_SECRET" not in (result.content or "")
    assert "MODEL_SECRET" not in tasks.get("final-redaction")["result_summary"]


def test_sensitive_environment_keys_are_removed(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AWS_SECRET")
    monkeypatch.setenv("CLIENT_SECRET", "CLIENT_SECRET_VALUE")

    environment = BashTool._safe_environment()

    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "CLIENT_SECRET" not in environment


def test_process_events_are_structured_and_display_failure_isolated(tmp_path: Path) -> None:
    database = Database(tmp_path / "events.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    approvals = StaticApprovalHandler(True)
    sink = RecordingSink()
    executor = ToolExecutor(
        ToolRegistry((EchoTool(),), model_message_budget=256), approvals, audit, event_sink=sink
    )
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("echo-event", "echo", {}),)),
            AssistantTurn("最终答案"),
        ]
    )

    result = run(
        AgentLoop(backend, executor, tasks, event_sink=sink).run(
            "展示过程", task_id="events"
        )
    )

    assert result.status is TaskStatus.SUCCESS
    event_types = [event.event_type for event in sink.events]
    assert event_types[0] == "task_started"
    assert "严格模式" in sink.events[0].message
    assert {
        "model_started",
        "model_finished",
        "tool_started",
        "safety_passed",
        "approval_auto_allowed",
        "tool_finished",
        "task_finished",
    }.issubset(event_types)
    assert all(event.task_id == "events" for event in sink.events)

    failing_sink = RecordingSink(fail=True)
    failing_executor = ToolExecutor(
        ToolRegistry((EchoTool(),), model_message_budget=256), approvals, audit, event_sink=failing_sink
    )
    failing_backend = FakeModelBackend([AssistantTurn("展示故障后仍成功")])
    result = run(
        AgentLoop(failing_backend, failing_executor, tasks, event_sink=failing_sink).run(
            "展示故障不应影响任务", task_id="events-failing-sink"
        )
    )
    assert result.status is TaskStatus.SUCCESS


def test_rejected_and_cancelled_tools_have_explicit_terminal_events(tmp_path: Path) -> None:
    database = Database(tmp_path / "terminal-events.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    sink = RecordingSink()
    approvals = StaticApprovalHandler(False)
    executor = ToolExecutor(
        ToolRegistry(
            build_builtin_tools(
                Settings(
                    workspace_root=tmp_path,
                    database_path=tmp_path / "terminal-events.sqlite3",
                    bash_path=None,
                ),
                ReviewMode.STRICT,
            ),
            256,
            ReviewMode.STRICT,
        ),
        approvals,
        audit,
        ReviewMode.STRICT,
        sink,
    )
    tasks.create("rejected-event", "拒绝写入", ReviewMode.STRICT)

    rejected = run(
        executor.execute(
            "rejected-event",
            ToolCall("rejected-write", "write", {"path": "denied.txt", "content": "x"}),
        )
    )

    assert rejected.is_error
    assert any(event.event_type == "tool_rejected" for event in sink.events)
    assert "耗时=" in next(event.message for event in sink.events if event.event_type == "tool_rejected")

    cancel_database = Database(tmp_path / "cancelled-events.sqlite3")
    cancel_database.initialize()
    cancel_tasks = TaskRepository(cancel_database)
    cancel_audit = AuditRepository(cancel_database)
    cancel_sink = RecordingSink()
    cancel_executor = ToolExecutor(
        ToolRegistry((CancelledTool(),), model_message_budget=256),
        StaticApprovalHandler(True),
        cancel_audit,
        event_sink=cancel_sink,
    )
    cancel_tasks.create("cancelled-event", "取消工具", ReviewMode.STRICT)

    with pytest.raises(asyncio.CancelledError):
        run(
            cancel_executor.execute(
                "cancelled-event", ToolCall("cancelled-call", "cancelled", {})
            )
        )

    assert any(event.event_type == "tool_cancelled" for event in cancel_sink.events)
    assert cancel_audit.list_tool_calls("cancelled-event")[0]["status"] == "cancelled"


def test_relaxed_mode_allows_raw_shell_after_each_approval(settings) -> None:
    pytest.importorskip("shutil").which("bash")
    if settings.bash_path is None:
        pytest.skip("当前环境没有可用 Git Bash")
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(settings.workspace_root, ReviewMode.RELAXED, approvals)
    tasks.create("relaxed", "执行脚本", ReviewMode.RELAXED)

    result = run(
        executor.execute(
            "relaxed",
            ToolCall(
                "bash-relaxed",
                "bash",
                {"command": "printf one | tr a-z A-Z > relaxed.txt; printf two >> relaxed.txt"},
            ),
        )
    )

    assert not result.is_error
    assert (settings.workspace_root / "relaxed.txt").read_text(encoding="utf-8") == "ONEtwo"
    assert len(approvals.requests) == 1
    assert audit.list_approvals("relaxed")[0]["decision_source"] == "human"


def test_relaxed_mode_denial_does_not_execute_raw_shell(settings) -> None:
    if settings.bash_path is None:
        pytest.skip("当前环境没有可用 Git Bash")
    approvals = StaticApprovalHandler(False)
    _, tasks, _, executor = build_components(settings.workspace_root, ReviewMode.RELAXED, approvals)
    tasks.create("relaxed-denied", "拒绝脚本", ReviewMode.RELAXED)

    result = run(
        executor.execute(
            "relaxed-denied",
            ToolCall(
                "bash-relaxed-denied",
                "bash",
                {"command": "printf should-not-exist > denied.txt"},
            ),
        )
    )

    assert result.is_error
    assert not (settings.workspace_root / "denied.txt").exists()
    assert len(approvals.requests) == 1


def test_full_access_allows_raw_shell_without_per_call_approval(settings) -> None:
    if settings.bash_path is None:
        pytest.skip("当前环境没有可用 Git Bash")
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(settings.workspace_root, ReviewMode.FULL_ACCESS, approvals)
    tasks.create("full-bash", "执行完全访问脚本", ReviewMode.FULL_ACCESS)

    result = run(
        executor.execute(
            "full-bash",
            ToolCall("bash-full", "bash", {"command": "printf full > full.txt"}),
        )
    )

    assert not result.is_error
    assert (settings.workspace_root / "full.txt").read_text(encoding="utf-8") == "full"
    assert approvals.requests == []
    assert audit.list_approvals("full-bash")[0]["decision_source"] == "mode"


def test_full_access_bash_keeps_output_environment_timeout_and_cancel_guards(
    monkeypatch, settings
) -> None:
    if settings.bash_path is None:
        pytest.skip("当前环境没有可用 Git Bash")
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(
        settings.workspace_root, ReviewMode.FULL_ACCESS, approvals
    )
    tasks.create("full-bash-guards", "验证完全访问 Bash 边界", ReviewMode.FULL_ACCESS)

    monkeypatch.setenv("CLIENT_SECRET", "FULL_ACCESS_SECRET")
    sensitive_output = run(
        executor.execute(
            "full-bash-guards",
            ToolCall(
                "bash-full-sensitive-output",
                "bash",
                {
                    "command": (
                        "printf '%s' '-----BEGIN PRIVATE KEY-----PARTIAL_PRIVATE_SECRET'; "
                        "printf 'x%.0s' {1..1000}"
                    )
                },
            ),
        )
    )
    assert sensitive_output.metadata["truncated"] is True
    assert "PARTIAL_PRIVATE_SECRET" not in sensitive_output.content
    assert "FULL_ACCESS_SECRET" not in sensitive_output.content

    environment_output = run(
        executor.execute(
            "full-bash-guards",
            ToolCall(
                "bash-full-environment",
                "bash",
                {"command": "printf '%s' \"${CLIENT_SECRET:-missing}\""},
            ),
        )
    )
    assert "FULL_ACCESS_SECRET" not in environment_output.content
    assert "missing" in environment_output.content

    timed_out = run(
        executor.execute(
            "full-bash-guards",
            ToolCall(
                "bash-full-timeout",
                "bash",
                {"command": "sleep 2", "timeout_seconds": 1},
            ),
        )
    )
    assert timed_out.is_error
    assert timed_out.metadata["timed_out"] is True

    async def cancel_running() -> ToolResult:
        cancel_event = asyncio.Event()
        execution = asyncio.create_task(
            executor.execute(
                "full-bash-guards",
                ToolCall("bash-full-cancel", "bash", {"command": "sleep 2"}),
                cancel_event,
            )
        )
        await asyncio.sleep(0.05)
        cancel_event.set()
        return await execution

    cancelled = run(cancel_running())
    assert cancelled.is_error
    assert cancelled.metadata["cancelled"] is True
    assert approvals.requests == []
    assert "PARTIAL_PRIVATE_SECRET" not in str(audit.list_tool_calls("full-bash-guards"))
    assert "FULL_ACCESS_SECRET" not in str(audit.list_tool_calls("full-bash-guards"))


def test_relaxed_mode_auto_allows_workspace_write_and_records_mode(settings) -> None:
    approvals = StaticApprovalHandler(False)
    _, tasks, audit, executor = build_components(settings.workspace_root, ReviewMode.RELAXED, approvals)
    tasks.create("relaxed-write", "写入文件", ReviewMode.RELAXED)

    result = run(
        executor.execute(
            "relaxed-write",
            ToolCall("write-relaxed", "write", {"path": "relaxed.txt", "content": "ok"}),
        )
    )

    assert not result.is_error
    assert approvals.requests == []
    assert audit.list_approvals("relaxed-write")[0]["decision_source"] == "mode"
    assert tasks.get("relaxed-write")["review_mode"] == ReviewMode.RELAXED.value


def test_full_access_requires_confirmation_before_task_creation(runtime) -> None:
    settings, _, tasks, _, _, _ = runtime
    backend = FakeModelBackend([AssistantTurn("不应调用")])
    full_runtime = build_runtime(
        settings,
        backend=backend,
        approvals=StaticApprovalHandler(False),
        review_mode=ReviewMode.FULL_ACCESS,
    )

    result = run(full_runtime.agent.run("完全访问", task_id="full-denied"))

    assert result.status is TaskStatus.CANCELLED
    assert tasks.get("full-denied") is None
    assert backend.call_count == 0


def test_full_access_runtime_defers_bash_probe_until_confirmation(monkeypatch, settings) -> None:
    def fail_probe(self) -> None:
        raise AssertionError("完全访问确认前不应探测 Bash")

    monkeypatch.setattr(BashTool, "validate_runtime", fail_probe)
    backend = FakeModelBackend([AssistantTurn("不应调用")])
    runtime = build_runtime(
        settings,
        backend=backend,
        approvals=StaticApprovalHandler(False),
        review_mode=ReviewMode.FULL_ACCESS,
    )

    result = run(runtime.agent.run("完全访问", task_id="full-deferred"))

    assert result.status is TaskStatus.CANCELLED
    assert runtime.tasks.get("full-deferred") is None
    assert backend.call_count == 0


def test_full_access_operates_isolated_external_file_and_records_mode(tmp_path: Path) -> None:
    external = tmp_path.parent / "full-access-isolated-target" / "nested"
    external.mkdir(parents=True)
    overwrite = external / "overwrite.txt"
    overwrite.write_text("旧内容", encoding="utf-8")
    dotdot_target = tmp_path / ".." / "full-access-isolated-target" / "nested" / "target.txt"
    approvals = StaticApprovalHandler(True)
    settings, tasks, audit, executor = build_components(tmp_path, ReviewMode.FULL_ACCESS, approvals)
    backend = FakeModelBackend(
        [
            AssistantTurn(
                "",
                (
                    ToolCall(
                        "write-full",
                        "write",
                        {"path": str(dotdot_target), "content": "full access"},
                    ),
                    ToolCall(
                        "overwrite-full",
                        "write",
                        {"path": str(overwrite), "content": "覆盖成功"},
                    ),
                    ToolCall(
                        "read-full",
                        "read",
                        {"path": str(external / "target.txt")},
                    ),
                    ToolCall(
                        "edit-full",
                        "edit",
                        {
                            "path": str(external / "target.txt"),
                            "old_text": "full access",
                            "new_text": "edited externally",
                        },
                    ),
                ),
            ),
            AssistantTurn("完全访问完成"),
        ]
    )

    result = run(
        AgentLoop(
            backend,
            executor,
            tasks,
            review_mode=ReviewMode.FULL_ACCESS,
            approvals=approvals,
        ).run("写入外部隔离目录", task_id="full-success")
    )

    assert result.status is TaskStatus.SUCCESS
    assert (external / "target.txt").read_text(encoding="utf-8") == "edited externally"
    assert overwrite.read_text(encoding="utf-8") == "覆盖成功"
    assert tasks.get("full-success")["review_mode"] == ReviewMode.FULL_ACCESS.value
    sources = {row["decision_source"] for row in audit.list_approvals("full-success")}
    assert sources == {"human", "mode"}
    assert len(approvals.requests) == 1
    assert approvals.requests[0].confirmation_token == "FULL-ACCESS"
    assert settings.workspace_root.exists()


def test_full_access_read_redacts_sensitive_result(tmp_path: Path) -> None:
    sensitive = tmp_path.parent / ".env.full-access-test"
    sensitive.write_text("OPENAI_API_KEY=TEST_SECRET\n", encoding="utf-8")
    approvals = StaticApprovalHandler(True)
    _, tasks, _, executor = build_components(tmp_path, ReviewMode.FULL_ACCESS, approvals)
    backend = FakeModelBackend(
        [
            AssistantTurn(
                "",
                (ToolCall("read-full-sensitive", "read", {"path": str(sensitive)}),),
            ),
            AssistantTurn("已读取"),
        ]
    )

    result = run(
        AgentLoop(
            backend,
            executor,
            tasks,
            review_mode=ReviewMode.FULL_ACCESS,
            approvals=approvals,
        ).run("读取隔离敏感文件", task_id="full-sensitive")
    )

    assert result.status is TaskStatus.SUCCESS
    tool_message = backend.messages[1][-1].content
    assert "TEST_SECRET" not in tool_message
    assert "[已脱敏]" in tool_message


def test_full_access_sensitive_edit_does_not_return_diff(tmp_path: Path) -> None:
    sensitive = tmp_path.parent / ".env.full-access-edit-test"
    sensitive.write_text("arbitrary-sensitive-value\n", encoding="utf-8")
    approvals = StaticApprovalHandler(True)
    _, tasks, _, executor = build_components(tmp_path, ReviewMode.FULL_ACCESS, approvals)
    tasks.create("full-sensitive-edit", "修改隔离敏感文件", ReviewMode.FULL_ACCESS)

    result = run(
        executor.execute(
            "full-sensitive-edit",
            ToolCall(
                "edit-full-sensitive",
                "edit",
                {
                    "path": str(sensitive),
                    "old_text": "arbitrary-sensitive-value",
                    "new_text": "changed-sensitive-value",
                },
            ),
        )
    )

    assert not result.is_error
    assert "arbitrary-sensitive-value" not in result.content
    assert "changed-sensitive-value" not in result.content
    assert "[已脱敏]" in result.content
    assert result.metadata["path"] == "[敏感路径]"


def test_legacy_database_migrates_mode_and_approval_source(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                request_text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_summary TEXT,
                error_type TEXT,
                error_message TEXT
            );
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                request_summary TEXT NOT NULL,
                decision TEXT NOT NULL,
                decided_at TEXT NOT NULL
            );
            INSERT INTO tasks(task_id, request_text, status, created_at)
            VALUES ('legacy-task', 'summary', 'success', 'now');
            INSERT INTO approvals(
                approval_id, task_id, tool_call_id, action_type,
                request_summary, decision, decided_at
            ) VALUES ('approval-1', 'legacy-task', 'call-1', 'write', 'safe', 'approved', 'now');
            """
        )

    database = Database(path)
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)

    assert tasks.get("legacy-task")["review_mode"] == ReviewMode.STRICT.value
    assert audit.list_approvals("legacy-task")[0]["decision_source"] == "legacy"
    assert tasks.create("new-relaxed", "new", ReviewMode.RELAXED)
