"""存储与 Agent Loop 测试：关联 TaskRepository、AuditRepository 和 ToolExecutor，验证任务生命周期。"""

from __future__ import annotations

import asyncio

from likai_nexus.errors import TaskAlreadyExistsError
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.schemas import AssistantTurn, TaskStatus, ToolCall


def run(coro):
    """在无 pytest-asyncio 依赖时运行 Agent Loop。"""

    return asyncio.run(coro)


def test_task_id_is_idempotent(runtime) -> None:
    _, _, tasks, _, _, _ = runtime
    assert tasks.create("same", "first")
    assert not tasks.create("same", "second")
    assert "任务请求：字节数=5，sha256=" in tasks.get("same")["request_text"]


def test_provider_tool_call_id_is_unique_per_task_not_globally(runtime) -> None:
    settings, _, tasks, audit, _, executor = runtime
    (settings.workspace_root / "same.txt").write_text("same", encoding="utf-8")
    tasks.create("task-a", "read a")
    tasks.create("task-b", "read b")
    call = ToolCall("provider-call-1", "read", {"path": "same.txt"})

    first = run(executor.execute("task-a", call))
    second = run(executor.execute("task-b", call))

    assert not first.is_error
    assert not second.is_error
    assert len(audit.list_tool_calls("task-a")) == 1
    assert len(audit.list_tool_calls("task-b")) == 1


def test_running_tasks_are_recovered(runtime) -> None:
    _, _, tasks, _, _, _ = runtime
    tasks.create("running", "recover")
    tasks.set_status("running", TaskStatus.RUNNING)
    assert tasks.recover_running() == 1
    row = tasks.get("running")
    assert row["status"] == TaskStatus.FAILED.value
    assert row["error_type"] == "InterruptedTask"


def test_agent_loop_reads_then_returns_final_answer(runtime) -> None:
    settings, _, tasks, audit, _, executor = runtime
    (settings.workspace_root / "README.txt").write_text("hello", encoding="utf-8")
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("read-1", "read", {"path": "README.txt"}),)),
            AssistantTurn("读取完成：hello"),
        ]
    )
    result = run(AgentLoop(backend, executor, tasks, max_turns=5).run("读取 README.txt", task_id="loop-1"))
    assert result.status is TaskStatus.SUCCESS
    assert result.content == "读取完成：hello"
    assert backend.call_count == 2
    assert len(audit.list_tool_calls("loop-1")) == 1
    assert tasks.get("loop-1")["status"] == TaskStatus.SUCCESS.value


def test_agent_loop_unknown_tool_is_returned_to_model(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("unknown-1", "unknown", {}),)),
            AssistantTurn("已收到工具错误，无法执行未知工具"),
        ]
    )
    result = run(AgentLoop(backend, executor, tasks, max_turns=5).run("测试未知工具", task_id="loop-unknown"))
    assert result.status is TaskStatus.SUCCESS
    assert "未知工具" in backend.messages[1][-1].content


def test_agent_loop_marks_model_failure(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    backend = FakeModelBackend([])
    result = run(AgentLoop(backend, executor, tasks).run("触发模型错误", task_id="loop-error"))
    assert result.status is TaskStatus.FAILED
    assert "模型调用失败" in result.error_message
    assert tasks.get("loop-error")["error_type"] == "ModelBackendError"


def test_agent_loop_honors_cancellation(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    cancel = asyncio.Event()
    cancel.set()
    backend = FakeModelBackend([AssistantTurn("不应调用")])
    result = run(
        AgentLoop(backend, executor, tasks).run("取消任务", task_id="loop-cancel", cancel_event=cancel)
    )
    assert result.status is TaskStatus.CANCELLED
    assert backend.call_count == 0
    assert tasks.get("loop-cancel")["status"] == TaskStatus.CANCELLED.value


def test_agent_loop_cancels_backend_that_is_already_running(runtime) -> None:
    _, _, tasks, _, _, executor = runtime

    class BlockingBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, messages, tools, cancel_event=None):
            self.started.set()
            await cancel_event.wait()
            raise asyncio.CancelledError

    async def scenario():
        cancel = asyncio.Event()
        backend = BlockingBackend()
        running = asyncio.create_task(
            AgentLoop(backend, executor, tasks).run(
                "运行中取消", task_id="loop-running-cancel", cancel_event=cancel
            )
        )
        await backend.started.wait()
        cancel.set()
        return await running

    result = run(scenario())

    assert result.status is TaskStatus.CANCELLED
    assert tasks.get("loop-running-cancel")["status"] == TaskStatus.CANCELLED.value


def test_agent_loop_fails_terminally_when_tool_audit_rejects_duplicate_id(runtime) -> None:
    settings, _, tasks, audit, _, executor = runtime
    (settings.workspace_root / "duplicate.txt").write_text("ok", encoding="utf-8")
    backend = FakeModelBackend(
        [
            AssistantTurn("", (ToolCall("same-call", "read", {"path": "duplicate.txt"}),)),
            AssistantTurn("", (ToolCall("same-call", "read", {"path": "duplicate.txt"}),)),
        ]
    )

    result = run(
        AgentLoop(backend, executor, tasks, max_turns=5).run(
            "重复调用编号", task_id="loop-duplicate-call"
        )
    )

    assert result.status is TaskStatus.FAILED
    assert tasks.get("loop-duplicate-call")["status"] == TaskStatus.FAILED.value
    assert len(audit.list_tool_calls("loop-duplicate-call")) == 1


def test_agent_loop_stops_after_max_turns(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    backend = FakeModelBackend(
        [AssistantTurn("", (ToolCall(f"read-{index}", "read", {"path": "missing.txt"}),)) for index in range(3)]
    )
    result = run(
        AgentLoop(backend, executor, tasks, max_turns=2).run("超过轮数", task_id="loop-max-turns")
    )
    assert result.status is TaskStatus.FAILED
    assert "轮数超过上限" in result.error_message
    assert tasks.get("loop-max-turns")["error_type"] == "MaxTurnsExceeded"


def test_agent_loop_rejects_duplicate_task(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    tasks.create("duplicate", "old")
    backend = FakeModelBackend([AssistantTurn("不会执行")])
    try:
        run(AgentLoop(backend, executor, tasks).run("new", task_id="duplicate"))
    except TaskAlreadyExistsError:
        pass
    else:
        raise AssertionError("重复 task_id 应该抛出 TaskAlreadyExistsError")
