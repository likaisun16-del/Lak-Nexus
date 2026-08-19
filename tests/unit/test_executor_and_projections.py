"""执行器与投影测试：覆盖扩展工具、审计安全、事件和通用展示边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_support import (
    CancelledTool,
    DisplayProjectionTool,
    EchoTool,
    MetadataTool,
    MismatchedTool,
    RecordingSink,
    build_components,
    run,
)

from likai_nexus.config import Settings
from likai_nexus.errors import ConfigError
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.schemas import AssistantTurn, TaskStatus
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.safety.redaction import redact_text
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository
from likai_nexus.tools.builtin import build_builtin_tools
from likai_nexus.tools.builtin.bash import BashTool
from likai_nexus.tools.contracts import ToolCall
from likai_nexus.tools.registry import ToolRegistry


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


def test_agent_prompt_mentions_default_script_directory(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    backend = FakeModelBackend([AssistantTurn("脚本任务完成")])

    result = run(AgentLoop(backend, executor, tasks).run("生成脚本", task_id="script-prompt"))

    assert result.status is TaskStatus.SUCCESS
    assert "script/" in backend.messages[0][0].content


def test_untrusted_tool_call_fields_are_safe_before_audit_persistence(tmp_path: Path) -> None:
    approvals = StaticApprovalHandler(True)
    _, tasks, audit, executor = build_components(tmp_path, ReviewMode.STRICT, approvals)
    tasks.create("untrusted-audit", "安全审计字段", ReviewMode.STRICT)

    known = run(
        executor.execute(
            "untrusted-audit",
            ToolCall("token=KNOWN_CALL_SECRET", "write", {"path": "known.txt", "content": "safe"}),
        )
    )
    known_invalid = run(
        executor.execute(
            "untrusted-audit",
            ToolCall("token=KNOWN_ARGUMENT_CALL_SECRET", "write", {"token=KNOWN_ARGUMENT_SECRET": "value"}),
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
    backend = FakeModelBackend([AssistantTurn("", (ToolCall(call_id, tool_name, {}),))])

    result = run(
        AgentLoop(backend, executor, tasks, max_turns=5, event_sink=sink).run(
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
        [AssistantTurn("", (ToolCall(call_id, "write", {"path": "safe.txt", "content": "safe"}),))]
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
    registry = ToolRegistry((EchoTool(),), model_message_budget=256, review_mode=ReviewMode.RELAXED)
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
                ToolExecutor(registry, StaticApprovalHandler(True), audit, requested_mode)

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

    result = run(executor.execute("metadata-task", ToolCall("metadata-call", "metadata", {})))

    assert not result.is_error
    assert "custom_cursor" in result.content
    assert "custom_complete" in result.content


def test_registry_rejects_duplicate_tool_names() -> None:
    with pytest.raises(ConfigError, match="工具名称重复"):
        ToolRegistry((EchoTool(), EchoTool()))


def test_sensitive_json_and_assignment_values_are_redacted() -> None:
    value = (
        "TOKEN=TEST_SECRET AWS_ACCESS_KEY_ID=AWS_SECRET "
        '{"password": "JSON_SECRET", "access_key": "JSON_ACCESS_SECRET", "safe": "visible"} '
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
    redacted = redact_text("-----BEGIN PRIVATE KEY-----\nPARTIAL_PRIVATE_SECRET")

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
        AgentLoop(backend, executor, tasks, max_turns=5, event_sink=sink).run(
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
    model_started = [event for event in sink.events if event.event_type == "model_started"]
    assert [event.metadata["turn_number"] for event in model_started] == [1, 2]
    assert all(event.metadata["max_turns"] == 5 for event in model_started)
    echo_finished = next(event for event in sink.events if event.event_type == "tool_finished")
    assert "result" not in echo_finished.metadata

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


@pytest.mark.parametrize("mode", ["raise", "cycle", "weird", "deep"])
def test_display_projection_failure_cannot_change_successful_tool(tmp_path: Path, mode: str) -> None:
    database = Database(tmp_path / f"display-{mode}.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    tool = DisplayProjectionTool(mode)
    executor = ToolExecutor(ToolRegistry((tool,), model_message_budget=256), StaticApprovalHandler(True), audit)
    tasks.create(f"display-{mode}", "展示投影故障测试", ReviewMode.STRICT)

    result = run(executor.execute(f"display-{mode}", ToolCall(f"display-call-{mode}", tool.name, {})))

    assert not result.is_error
    assert tool.execute_calls == 1
    assert tool.display_calls == 1
    assert audit.list_tool_calls(f"display-{mode}")[0]["status"] == "success"


@pytest.mark.parametrize("mode", ["argument-raise", "argument-weird", "argument-deep"])
def test_display_arguments_failure_cannot_prevent_successful_tool(tmp_path: Path, mode: str) -> None:
    database = Database(tmp_path / f"display-arguments-{mode}.sqlite3")
    database.initialize()
    tasks = TaskRepository(database)
    audit = AuditRepository(database)
    tool = DisplayProjectionTool(mode)
    executor = ToolExecutor(ToolRegistry((tool,), model_message_budget=256), StaticApprovalHandler(True), audit)
    task_id = f"display-arguments-{mode}"
    tasks.create(task_id, "调用展示故障测试", ReviewMode.STRICT)

    result = run(executor.execute(task_id, ToolCall(f"call-{mode}", tool.name, {})))

    assert not result.is_error
    assert tool.execute_calls == 1
    assert audit.list_tool_calls(task_id)[0]["status"] == "success"


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_events_expose_safe_command_and_result_projection(tmp_path: Path) -> None:
    settings, tasks, audit, executor = build_components(tmp_path, ReviewMode.FULL_ACCESS, StaticApprovalHandler(True))
    sink = RecordingSink()
    executor.event_sink = sink
    tasks.create("bash-display", "展示 Bash 结果", ReviewMode.FULL_ACCESS)

    result = run(
        executor.execute(
            "bash-display",
            ToolCall(
                "bash-display-call",
                "bash",
                {"command": "printf 'stdout\\n'; printf 'stderr\\n' >&2", "timeout_seconds": 3},
            ),
        )
    )

    assert not result.is_error
    started = next(event for event in sink.events if event.event_type == "tool_started")
    assert "printf 'stdout" in started.metadata["invocation"]
    assert "超时：3 秒" in started.metadata["invocation"]
    assert "sha256" not in started.metadata["invocation"]
    finished = next(event for event in sink.events if event.event_type == "tool_finished")
    display = {field["label"]: field["value"] for field in finished.metadata["result"]["fields"]}
    assert display["stdout"] == "stdout\n"
    assert display["stderr"] == "stderr\n"
    assert display["退出码"] == 0
    assert "输出状态" not in display
    assert "stdout" not in str(audit.list_tool_calls("bash-display"))
    assert "stderr" not in str(audit.list_tool_calls("bash-display"))
    assert settings.workspace_root.exists()


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
                    project_root=tmp_path,
                    database_path=tmp_path.parent / f"{tmp_path.name}-terminal-events.sqlite3",
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
        run(cancel_executor.execute("cancelled-event", ToolCall("cancelled-call", "cancelled", {})))

    assert any(event.event_type == "tool_cancelled" for event in cancel_sink.events)
    assert cancel_audit.list_tool_calls("cancelled-event")[0]["status"] == "cancelled"
