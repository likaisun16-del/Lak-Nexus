"""Session CLI 集成测试：验证会话管理命令不依赖模型密钥且遵守删除确认。"""

from __future__ import annotations

import re

from likai_nexus.channels.cli import main
from likai_nexus.runtime import build_preference_store
from likai_nexus.storage.commit_repository import CommitRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.session_repository import SessionRepository
from likai_nexus.storage.task_repository import TaskRepository


def test_session_cli_new_list_and_delete(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)

    assert main(["session", "new", "--title", "CLI 会话"]) == 0
    created = capsys.readouterr().out
    match = re.search(r"Session ([0-9a-f]+)", created)
    assert match is not None
    session_id = match.group(1)

    assert main(["session", "list"]) == 0
    assert session_id in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda prompt: "NO")
    assert main(["session", "delete", session_id]) == 1
    assert "已取消删除" in capsys.readouterr().out

    assert main(["session", "delete", session_id, "--confirm"]) == 0
    assert session_id in capsys.readouterr().out


def test_session_cli_history_continue_commit_and_switch(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    database = Database(settings.database_path)
    database.initialize()
    sessions = SessionRepository(database)
    tasks = TaskRepository(database)
    commits = CommitRepository(database)
    first = sessions.create("第一会话")
    second = sessions.create("第二会话")
    tasks.create("cli-task", "CLI 代码任务")
    user = sessions.add_message(
        first["session_id"],
        "user",
        "CLI 代码任务",
        parent_message_id=None,
        task_id="cli-task",
        execution_status="success",
    )
    assistant = sessions.add_message(
        first["session_id"],
        "assistant",
        "CLI 任务完成",
        parent_message_id=user["message_id"],
        task_id="cli-task",
    )
    commits.record("cli-task", "a" * 40)
    preferences, _ = build_preference_store(settings)
    preferences.save_active_session_id(first["session_id"])

    assert main(["session", "history", first["session_id"]]) == 0
    history_output = capsys.readouterr().out
    assert "Task=cli-task" in history_output
    assert "Commit SHA=" + "a" * 40 in history_output
    assert "不会撤销未提交文件" in history_output

    assert main(["session", "commit", assistant["message_id"]]) == 0
    commit_output = capsys.readouterr().out
    assert "Task=cli-task" in commit_output
    assert "Commit SHA=" + "a" * 40 in commit_output
    assert "数据库、网络、系统或外部服务副作用" in commit_output

    assert main(["session", "switch", second["session_id"]]) == 0
    capsys.readouterr()
    assert main(["session", "continue-from", assistant["message_id"]]) == 2
    error_output = capsys.readouterr().err
    assert "其他 Session" in error_output
    assert preferences.load_active_session_id() == second["session_id"]

    assert main(["session", "switch", first["session_id"]]) == 0
    capsys.readouterr()
    assert main(["session", "continue-from", assistant["message_id"]]) == 0
    assert "活动分支已切换" in capsys.readouterr().out
    assert preferences.load_active_session_id() == first["session_id"]
