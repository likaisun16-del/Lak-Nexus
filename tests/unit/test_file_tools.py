"""文件工具测试：关联 ToolExecutor、路径解析和审批流程，覆盖 read、write、edit 的边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from likai_nexus.errors import AuditError
from likai_nexus.executor.tools.bash import BashTool
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


def test_read_long_line_advances_byte_cursor(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_bytes(b"abcdefghij\n")
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=5)

    first = run(tool.execute(tool.validate({"path": "long.txt"})))
    second = run(
        tool.execute(
            tool.validate(
                {
                    "path": "long.txt",
                    "offset": first.metadata["next_offset"],
                    "byte_offset": first.metadata["next_byte_offset"],
                }
            )
        )
    )
    third = run(
        tool.execute(
            tool.validate(
                {
                    "path": "long.txt",
                    "offset": second.metadata["next_offset"],
                    "byte_offset": second.metadata["next_byte_offset"],
                }
            )
        )
    )

    assert first.content.startswith("abcde")
    assert second.content.startswith("fghij")
    assert third.content == "\n"
    assert first.metadata["next_byte_offset"] > 0
    assert second.metadata["next_byte_offset"] > first.metadata["next_byte_offset"]
    assert len(first.content.encode()) <= 5
    assert first.metadata["next_cursor"] == "0:5"


def test_read_multibyte_cursor_is_valid_utf8(tmp_path: Path) -> None:
    (tmp_path / "中文.txt").write_text("你好世界\n", encoding="utf-8")
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=4)

    output = run(tool.execute(tool.validate({"path": "中文.txt"})))

    assert output.content.startswith("你")
    assert "�" not in output.content
    assert output.metadata["next_byte_offset"] == len("你".encode())
    assert len(output.content.encode()) <= 4


def test_read_minimum_budget_cursor_progresses_to_next_line(tmp_path: Path) -> None:
    (tmp_path / "mixed.txt").write_bytes("a\n中\n".encode())
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=4)

    first = run(tool.execute(tool.validate({"path": "mixed.txt"})))
    second = run(
        tool.execute(
            tool.validate(
                {
                    "path": "mixed.txt",
                    "offset": first.metadata["next_offset"],
                    "byte_offset": first.metadata["next_byte_offset"],
                }
            )
        )
    )

    assert first.content == "a\n"
    assert first.metadata["next_cursor"] == "1:0"
    assert second.content == "中\n"


def test_read_rejects_budget_that_cannot_advance_utf8_cursor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="至少为 4"):
        ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=1)


def test_read_rejects_mid_codepoint_cursor(tmp_path: Path) -> None:
    (tmp_path / "中文.txt").write_text("你好", encoding="utf-8")
    tool = ReadFileTool(WorkspacePathResolver(tmp_path), max_lines=20, max_bytes=4)

    with pytest.raises(Exception, match="UTF-8 字符边界"):
        run(
            tool.execute(
                tool.validate({"path": "中文.txt", "offset": 0, "byte_offset": 1})
            )
        )


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
    assert "内容 sha256=" in approvals.requests[0].summary
    assert "new sha256=" in approvals.requests[1].summary
    assert "预览" in approvals.requests[1].summary
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


def test_write_rejects_approval_when_target_changes(runtime) -> None:
    settings, _, tasks, audit, _, executor = runtime
    tasks.create("task-race", "approval race")
    target = settings.workspace_root / "race.txt"
    target.write_text("before", encoding="utf-8")

    class MutatingApproval:
        async def request(self, request) -> bool:
            target.write_text("changed outside approval", encoding="utf-8")
            return True

    executor.approvals = MutatingApproval()
    result = run(
        executor.execute(
            "task-race",
            ToolCall("write-race", "write", {"path": "race.txt", "content": "agent"}),
        )
    )

    assert result.is_error
    assert "发生变化" in result.content
    assert target.read_text(encoding="utf-8") == "changed outside approval"
    assert audit.list_tool_calls("task-race")[0]["status"] == "rejected"


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


def test_task_and_approval_audits_store_summaries_only(runtime) -> None:
    _, database, tasks, audit, _, executor = runtime
    sentinel = "UNLABELED_SECRET_SENTINEL"
    tasks.create("task-sentinel", sentinel)
    result = run(
        executor.execute(
            "task-sentinel",
            ToolCall("write-sentinel", "write", {"path": "normal.txt", "content": sentinel}),
        )
    )

    assert not result.is_error
    with database.connection() as connection:
        rows = connection.execute("SELECT request_text FROM tasks").fetchall()
        approvals = connection.execute("SELECT request_summary FROM approvals").fetchall()
    assert sentinel not in str(rows)
    assert sentinel not in str(approvals)
    assert sentinel not in str(audit.list_tool_calls("task-sentinel"))


def test_bash_approval_audit_does_not_store_raw_arguments(runtime) -> None:
    _, database, tasks, audit, _, executor = runtime
    sentinel = "UNLABELED_SECRET_SENTINEL"
    tasks.create("task-bash-audit", "bash audit")

    result = run(
        executor.execute(
            "task-bash-audit",
            ToolCall(
                "bash-audit",
                "bash",
                {"command": f"rg {sentinel} README.md", "timeout_seconds": 1},
            ),
        )
    )

    with database.connection() as connection:
        approvals = connection.execute("SELECT request_summary FROM approvals").fetchall()
    assert result.is_error
    assert sentinel not in result.content
    assert sentinel not in str(approvals)
    assert sentinel not in str(audit.list_tool_calls("task-bash-audit"))


@pytest.mark.skipif(BashTool.discover_bash_path() is None, reason="当前环境没有可用 Git Bash")
def test_bash_recursive_rg_excludes_sensitive_files(runtime) -> None:
    settings, database, tasks, audit, _, executor = runtime
    sentinel = "UNLABELED_REVIEW_SENTINEL_3"
    nested = settings.workspace_root / "nested"
    nested.mkdir()
    (nested / "credentials.json").write_text(sentinel, encoding="utf-8")
    (nested / ".env.local").write_text(sentinel, encoding="utf-8")
    (nested / "private.pem").write_text(sentinel, encoding="utf-8")
    private_dir = nested / "private"
    private_dir.mkdir()
    (private_dir / "notes.txt").write_text(sentinel, encoding="utf-8")
    (nested / "normal.txt").write_text("normal", encoding="utf-8")
    tasks.create("task-rg-protected", "recursive search")

    result = run(
        executor.execute(
            "task-rg-protected",
            ToolCall("rg-recursive", "bash", {"command": f"rg {sentinel} ."}),
        )
    )
    files = run(
        executor.execute(
            "task-rg-protected",
            ToolCall("rg-files", "bash", {"command": "rg --files ."}),
        )
    )

    assert result.is_error
    assert sentinel not in result.content
    assert "credentials.json" not in files.content
    assert ".env.local" not in files.content
    assert "private.pem" not in files.content
    assert "private/notes.txt" not in files.content
    with database.connection() as connection:
        rows = connection.execute("SELECT request_summary FROM approvals").fetchall()
    assert sentinel not in str(rows)
    assert sentinel not in str(audit.list_tool_calls("task-rg-protected"))


def test_approval_audit_failure_finishes_tool_as_failed(runtime) -> None:
    _, _, tasks, audit, _, executor = runtime
    tasks.create("task-audit-failure", "audit failure")

    def fail_record(*args, **kwargs):
        raise RuntimeError("simulated approval audit outage")

    audit.record_approval = fail_record
    with pytest.raises(AuditError):
        run(
            executor.execute(
                "task-audit-failure",
                ToolCall("write-audit-failure", "write", {"path": "x.txt", "content": "x"}),
            )
        )

    assert audit.list_tool_calls("task-audit-failure")[0]["status"] == "failed"


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
