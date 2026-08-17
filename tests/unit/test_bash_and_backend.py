"""Bash 与模型协议测试：验证 BashTool 的执行边界，并关联 OpenAI Backend 的协议转换。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from likai_nexus.errors import ConfigError, ModelBackendError, ToolExecutionError
from likai_nexus.executor.service import ToolExecutor
from likai_nexus.executor.tools.bash import BashTool
from likai_nexus.models.openai_backend import OpenAICompatibleBackend
from likai_nexus.orchestrator.schemas import ChatMessage, ToolCall
from likai_nexus.runtime import build_runtime
from likai_nexus.safety.command_policy import CommandPolicy


def run(coro):
    """在无 pytest-asyncio 依赖时运行 Bash 异步测试。"""

    return asyncio.run(coro)


def test_openai_response_is_converted(settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        api_key="not-real",
    )
    backend = OpenAICompatibleBackend(settings)
    result = backend._parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "需要读取",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                            }
                        ],
                    }
                }
            ]
        }
    )
    assert result.content == "需要读取"
    assert result.tool_calls[0] == ToolCall("call-1", "read", {"path": "README.md"})


def test_openai_response_errors_are_specific(settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        api_key="not-real",
    )
    with pytest.raises(ModelBackendError, match="缺少 choices"):
        OpenAICompatibleBackend(settings)._parse_response({})


def test_bash_rejects_wsl_path_from_windows_path(monkeypatch, settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        bash_path=None,
    )
    monkeypatch.setattr(
        "likai_nexus.executor.tools.bash.shutil.which",
        lambda name: r"C:\Users\test\WindowsApps\bash.exe",
    )
    monkeypatch.setattr(BashTool, "discover_bash_path", classmethod(lambda cls: None))

    with pytest.raises(ToolExecutionError, match="WSL"):
        BashTool(settings, CommandPolicy())._find_bash()


def test_runtime_rejects_wsl_bash_before_starting_tasks(monkeypatch, settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        bash_path=None,
    )
    monkeypatch.setattr(
        "likai_nexus.executor.tools.bash.shutil.which",
        lambda name: r"C:\Users\test\WindowsApps\bash.exe",
    )
    monkeypatch.setattr(BashTool, "discover_bash_path", classmethod(lambda cls: None))

    with pytest.raises(ConfigError, match="WSL"):
        build_runtime(settings)


def test_bash_truncation_marker_stays_inside_budget(settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        max_output_bytes=64,
    )
    tool = BashTool(settings, CommandPolicy())

    message = tool._bounded_message("Bash 执行成功：退出码 0\n", "x" * 500, True)

    assert len(message.encode()) <= 64
    assert "输出已截断" in message


def test_model_tool_message_budget_preserves_safe_status() -> None:
    message = ToolExecutor._model_content(
        "x" * 500,
        {"next_cursor": "0:5", "truncated": True, "bytes": 500},
        64,
    )

    assert len(message.encode()) <= 64
    assert "next_cursor" in message
    assert "truncated" in message


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_runs_pwd_with_approval(settings) -> None:
    from likai_nexus.safety.command_policy import CommandPolicy

    tool = BashTool(settings, CommandPolicy())
    arguments = tool.validate({"command": "pwd"})
    tool.check_safety(arguments)
    result = run(tool.execute(arguments))
    assert not result.is_error
    assert result.metadata["exit_code"] == 0


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_captures_nonzero_exit_and_truncates_output(settings) -> None:
    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        bash_path=settings.bash_path,
        max_output_bytes=64,
    )
    output_file = settings.workspace_root / "output.txt"
    output_file.write_text("x\n" * 100, encoding="utf-8")
    tool = BashTool(settings, CommandPolicy())
    success = run(tool.execute(tool.validate({"command": "rg x output.txt"})))
    assert not success.is_error
    assert success.metadata["exit_code"] == 0
    assert success.metadata["truncated"] is True
    failure = run(tool.execute(tool.validate({"command": "pytest missing-test-file.py"})))
    assert failure.is_error is True
    assert failure.metadata["exit_code"] != 0


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_representative_commands_succeed(settings) -> None:
    repository_settings = settings.__class__(
        workspace_root=Path.cwd(),
        database_path=settings.database_path,
        bash_path=settings.bash_path,
    )
    tool = BashTool(repository_settings, CommandPolicy())

    for command in ("pwd", "git status --short", "python -m compileall src"):
        result = run(tool.execute(tool.validate({"command": command})))
        assert not result.is_error, command
        assert result.metadata["exit_code"] == 0, command


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_timeout_terminates_process(settings) -> None:
    tool = BashTool(settings, CommandPolicy())

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            settings.bash_path,
            "-lc",
            "sleep 2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return await tool._communicate(process, 1, None)

    _, _, reason, _ = run(scenario())
    assert reason == "timeout"


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_cancellation_terminates_running_process(settings) -> None:
    tool = BashTool(settings, CommandPolicy())

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            settings.bash_path,
            "-lc",
            "sleep 2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cancel = asyncio.Event()
        communication = asyncio.create_task(tool._communicate(process, 10, cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        return await communication

    _, _, reason, _ = run(scenario())
    assert reason == "cancelled"


def test_openai_message_payload_keeps_tool_calls() -> None:
    message = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall("call-1", "read", {"path": "README.md"}),),
    )
    payload = OpenAICompatibleBackend._message_to_payload(message)
    assert payload["tool_calls"][0]["function"]["name"] == "read"
