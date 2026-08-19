"""审查模式与数据库迁移测试：覆盖 strict、relaxed、full-access 的生命周期边界。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from test_support import build_components, run

from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.schemas import AssistantTurn, TaskStatus
from likai_nexus.runtime import build_runtime
from likai_nexus.safety.approval import StaticApprovalHandler
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.audit_repository import AuditRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.task_repository import TaskRepository
from likai_nexus.tools.builtin.bash import BashTool
from likai_nexus.tools.contracts import ToolCall, ToolResult


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
            ToolCall("bash-full-timeout", "bash", {"command": "sleep 2", "timeout_seconds": 1}),
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
                    ToolCall("write-full", "write", {"path": str(dotdot_target), "content": "full access"}),
                    ToolCall("overwrite-full", "write", {"path": str(overwrite), "content": "覆盖成功"}),
                    ToolCall("read-full", "read", {"path": str(external / "target.txt")}),
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
            AssistantTurn("", (ToolCall("read-full-sensitive", "read", {"path": str(sensitive)}),)),
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
