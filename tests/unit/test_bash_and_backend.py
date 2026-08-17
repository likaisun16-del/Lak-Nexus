"""Bash 与模型协议测试：验证 BashTool 的执行边界，并关联 OpenAI Backend 的协议转换。"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from likai_nexus.errors import ModelBackendError
from likai_nexus.executor.tools.bash import BashTool
from likai_nexus.models.openai_backend import OpenAICompatibleBackend
from likai_nexus.orchestrator.schemas import ChatMessage, ToolCall


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


@pytest.mark.skipif(shutil.which("bash") is None, reason="当前环境没有 Bash")
def test_bash_runs_pwd_with_approval(settings) -> None:
    from likai_nexus.safety.command_policy import CommandPolicy

    tool = BashTool(settings, CommandPolicy())
    arguments = tool.validate({"command": "pwd"})
    tool.check_safety(arguments)
    result = run(tool.execute(arguments))
    assert not result.is_error
    assert result.metadata["exit_code"] == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="当前环境没有 Bash")
def test_bash_captures_nonzero_exit_and_truncates_output(settings) -> None:
    from likai_nexus.safety.command_policy import CommandPolicy

    settings = settings.__class__(
        workspace_root=settings.workspace_root,
        database_path=settings.database_path,
        bash_path=settings.bash_path,
        max_output_bytes=32,
    )
    output_file = settings.workspace_root / "output.txt"
    output_file.write_text("x\n" * 100, encoding="utf-8")
    tool = BashTool(settings, CommandPolicy())
    success = run(tool.execute(tool.validate({"command": "rg x output.txt"})))
    assert success.metadata["truncated"] is True
    failure = run(tool.execute(tool.validate({"command": "pytest missing-test-file.py"})))
    assert failure.is_error is True
    assert failure.metadata["exit_code"] != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="当前环境没有 Bash")
def test_bash_timeout_terminates_process(settings) -> None:
    from likai_nexus.safety.command_policy import CommandPolicy

    tool = BashTool(settings, CommandPolicy())

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            shutil.which("bash"), "-lc", "sleep 2", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        return await tool._communicate(process, 1, None)

    _, _, reason, _ = run(scenario())
    assert reason == "timeout"


@pytest.mark.skipif(shutil.which("bash") is None, reason="当前环境没有 Bash")
def test_bash_cancellation_terminates_running_process(settings) -> None:
    from likai_nexus.safety.command_policy import CommandPolicy

    tool = BashTool(settings, CommandPolicy())

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            shutil.which("bash"),
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
