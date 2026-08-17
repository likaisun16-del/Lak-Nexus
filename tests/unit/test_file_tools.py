"""文件工具测试：覆盖 read、write、edit 的边界、审批和内容完整性。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from likai_nexus.executor.tools.edit_file import EditFileTool
from likai_nexus.executor.tools.read_file import ReadFileTool
from likai_nexus.orchestrator.schemas import ToolCall
from likai_nexus.safety.paths import WorkspacePathResolver


def run(coro):
    """在无 pytest-asyncio 依赖时运行异步工具测试。"""

    return asyncio.run(coro)


def test_read_supports_offset_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    # 使用字节写入固定 LF，避免 Windows 文本模式换行转换干扰偏移量断言。
    path.write_bytes(b"one\ntwo\nthree\n")
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=2, max_bytes=100)
    output = run(tool.execute(tool.validate({"path": "notes.txt", "limit": 2})))
    assert output.content.startswith("one\ntwo\n")
    assert output.metadata["truncated"] is True
    assert output.metadata["next_offset"] == 2

    next_output = run(
        tool.execute(tool.validate({"path": "notes.txt", "offset": 2, "limit": 2}))
    )
    assert next_output.content == "three\n"


def test_read_rejects_binary_and_directory(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe")
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=256)
    with pytest.raises(Exception, match="不是有效 UTF-8"):
        run(tool.execute(tool.validate({"path": "binary.bin"})))
    with pytest.raises(Exception, match="不是普通文件"):
        tool.check_safety(tool.validate({"path": "."}))
    with pytest.raises(Exception, match="目标不存在"):
        tool.check_safety(tool.validate({"path": "missing.txt"}))


def test_write_creates_parent_and_returns_summary(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    tasks.create("task-write", "create and edit")
    call = ToolCall("write-1", "write", {"path": "nested/new.txt", "content": "hello"})
    result = run(executor.execute("task-write", call))
    assert result.is_error is False


def test_write_and_edit_through_executor(runtime) -> None:
    settings, _, tasks, audit, approvals, executor = runtime
    tasks.create("task-write", "create and edit")
    write_call = ToolCall("write-1", "write", {"path": "nested/new.txt", "content": "hello\n"})
    write_result = run(executor.execute("task-write", write_call))
    assert not write_result.is_error
    assert (settings.workspace_root / "nested/new.txt").read_text(encoding="utf-8") == "hello\n"
    edit_call = ToolCall(
        "edit-1", "edit", {"path": "nested/new.txt", "old_text": "hello", "new_text": "world"}
    )
    edit_result = run(executor.execute("task-write", edit_call))
    assert not edit_result.is_error
    assert "world" in (settings.workspace_root / "nested/new.txt").read_text(encoding="utf-8")
    assert len(approvals.requests) == 2
    assert [row["status"] for row in audit.list_tool_calls("task-write")] == ["success", "success"]

    overwrite = ToolCall("write-2", "write", {"path": "nested/new.txt", "content": "overwritten"})
    overwrite_result = run(executor.execute("task-write", overwrite))
    assert not overwrite_result.is_error
    assert (settings.workspace_root / "nested/new.txt").read_text(encoding="utf-8") == "overwritten"


def test_write_denial_does_not_modify_file(runtime) -> None:
    settings, _, tasks, audit, approvals, executor = runtime
    tasks.create("task-deny", "deny write")
    approvals.approved = False
    call = ToolCall("write-deny", "write", {"path": "denied.txt", "content": "secret"})
    result = run(executor.execute("task-deny", call))
    assert result.is_error
    assert "未批准" in result.content
    assert not (settings.workspace_root / "denied.txt").exists()
    assert audit.list_tool_calls("task-deny")[0]["status"] == "rejected"


def test_edit_requires_unique_match(tmp_path: Path) -> None:
    path = tmp_path / "repeat.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(tmp_path))
    arguments = tool.validate({"path": "repeat.txt", "old_text": "same", "new_text": "new"})
    with pytest.raises(Exception, match="匹配不唯一"):
        run(tool.execute(arguments))
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_edit_reports_missing_match_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "missing-match.txt"
    path.write_text("original", encoding="utf-8")
    tool = EditFileTool(WorkspacePathResolver(tmp_path))
    arguments = tool.validate({"path": path.name, "old_text": "absent", "new_text": "new"})
    with pytest.raises(Exception, match="未找到匹配文本"):
        run(tool.execute(arguments))
    assert path.read_text(encoding="utf-8") == "original"


def test_audit_summary_does_not_store_write_content(runtime) -> None:
    _, _, tasks, audit, _, executor = runtime
    tasks.create("task-redact", "audit redaction")
    content = "token=super-secret-value"
    result = run(
        executor.execute(
            "task-redact", ToolCall("write-redact", "write", {"path": "secret.txt", "content": content})
        )
    )
    assert not result.is_error
    assert content not in str(audit.list_tool_calls("task-redact"))


def test_edit_preserves_bom_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbfold\r\nline\r\n")
    tool = EditFileTool(WorkspacePathResolver(tmp_path))
    arguments = tool.validate({"path": "bom.txt", "old_text": "old\n", "new_text": "new\n"})
    output = run(tool.execute(arguments))
    assert not output.is_error
    assert path.read_bytes() == b"\xef\xbb\xbfnew\r\nline\r\n"


def test_file_tools_reject_workspace_escape(runtime) -> None:
    _, _, tasks, _, _, executor = runtime
    tasks.create("task-path", "path test")
    result = run(executor.execute("task-path", ToolCall("read-out", "read", {"path": "../x"})))
    assert result.is_error
    assert "工作区外" in result.content
