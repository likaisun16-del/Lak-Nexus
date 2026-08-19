"""Session 树与 Git 关联测试：验证持久化、分支上下文、迁移和只读版本边界。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from likai_nexus.errors import SessionError
from likai_nexus.git import GitReadOnly
from likai_nexus.memory.session import SessionService
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.schemas import AssistantTurn, TaskStatus
from likai_nexus.storage.commit_repository import CommitRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.session_repository import SessionRepository
from likai_nexus.storage.task_repository import TaskRepository


def run(coro):
    """在不依赖 pytest-asyncio 的情况下运行 Session 异步问答。"""

    return asyncio.run(coro)


@pytest.fixture
def session_storage(tmp_path: Path):
    """创建独立 Session、Task 和 Commit 仓储。"""

    database = Database(tmp_path / "session.sqlite3")
    database.initialize()
    return database, SessionRepository(database), TaskRepository(database), CommitRepository(database)


def test_session_tree_preserves_old_branch_and_rejects_cross_session_parent(session_storage) -> None:
    _, sessions, tasks, commits = session_storage
    first = sessions.create("会话一")
    second = sessions.create("会话二")
    root = sessions.add_message(first["session_id"], "user", "根问题", parent_message_id=None)
    tasks.create("task-1", "根问题")
    answer = sessions.add_message(
        first["session_id"],
        "assistant",
        "根回答",
        parent_message_id=root["message_id"],
        task_id="task-1",
    )
    child = sessions.add_message(
        first["session_id"], "user", "旧分支", parent_message_id=answer["message_id"]
    )
    sessions.set_active_leaf(first["session_id"], answer["message_id"])
    branch = sessions.add_message(
        first["session_id"], "user", "新分支", parent_message_id=answer["message_id"]
    )

    assert [item["content"] for item in sessions.current_path(first["session_id"])] == [
        "根问题",
        "根回答",
        "新分支",
    ]
    branches = sessions.list_branches(first["session_id"])
    assert branches["branch_points"][0]["message_id"] == answer["message_id"]
    assert {leaf["message_id"] for leaf in branches["leaves"]} == {
        child["message_id"],
        branch["message_id"],
    }
    with pytest.raises(SessionError, match="其他 Session"):
        sessions.add_message(
            second["session_id"], "user", "越界", parent_message_id=root["message_id"]
        )

    commits.record("task-1", "a" * 40)
    assert commits.get_for_message(answer["message_id"])["commit_sha"] == "a" * 40
    assert sessions.delete(first["session_id"])
    assert sessions.get(first["session_id"]) is None
    assert sessions.get_message(root["message_id"]) is None
    assert tasks.get("task-1")["status"] == TaskStatus.PENDING.value
    assert commits.get_for_task("task-1")["commit_sha"] == "a" * 40


def test_linear_messages_are_migrated_to_parent_chain(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with Database(path).connection() as connection:
        connection.execute(
            "CREATE TABLE messages(message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO messages VALUES ('m1', 's1', 'user', '旧问题', '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO messages VALUES ('m2', 's1', 'assistant', '旧回答', '2026-01-01T00:00:01+00:00')"
        )

    database = Database(path)
    database.initialize()
    sessions = SessionRepository(database)

    assert [item["message_id"] for item in sessions.current_path("s1")] == ["m1", "m2"]
    assert sessions.get_message("m2")["parent_message_id"] == "m1"


def _session_service(runtime, backend: FakeModelBackend) -> SessionService:
    settings, database, tasks, _, approvals, executor = runtime
    agent = AgentLoop(
        backend,
        executor,
        tasks,
        max_turns=5,
        approvals=approvals,
    )
    return SessionService(
        SessionRepository(database),
        agent=agent,
        backend=backend,
        commits=CommitRepository(database),
        git_reader=GitReadOnly(settings.project_root),
    )


def test_session_ask_persists_visible_messages_and_generates_title(runtime) -> None:
    backend = FakeModelBackend([AssistantTurn("首轮回答"), AssistantTurn("首轮标题")])
    service = _session_service(runtime, backend)
    session = service.create()

    result = run(service.ask(session["session_id"], "首轮问题", task_id="session-task-1"))

    assert result.status is TaskStatus.SUCCESS
    assert result.assistant_message_id is not None
    assert result.title == "首轮标题"
    history = service.history(session["session_id"])
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "首轮问题"),
        ("assistant", "首轮回答"),
    ]
    assert [message.role for message in backend.messages[0]] == ["system", "user"]
    assert [message.role for message in backend.messages[1]] == ["system", "user", "assistant"]


def test_session_branch_context_excludes_sibling_messages(runtime) -> None:
    first_service = _session_service(
        runtime, FakeModelBackend([AssistantTurn("回答一"), AssistantTurn("标题一")])
    )
    session = first_service.create()
    first = run(first_service.ask(session["session_id"], "问题一", task_id="task-one"))

    second_backend = FakeModelBackend([AssistantTurn("回答二")])
    second_service = _session_service(runtime, second_backend)
    second = run(second_service.ask(session["session_id"], "问题二", task_id="task-two"))
    assert second.assistant_message_id is not None

    first_service.continue_from(first.assistant_message_id)
    branch_backend = FakeModelBackend([AssistantTurn("回答分支")])
    branch_service = _session_service(runtime, branch_backend)
    branch = run(branch_service.ask(session["session_id"], "问题分支", task_id="task-branch"))

    visible = [message.content for message in branch_backend.messages[0]]
    assert "问题一" in visible
    assert "回答一" in visible
    assert "问题分支" in visible
    assert "问题二" not in visible
    assert branch.assistant_message_id is not None


def test_session_failure_keeps_user_message_without_fake_assistant(runtime) -> None:
    service = _session_service(runtime, FakeModelBackend([]))
    session = service.create()

    result = run(service.ask(session["session_id"], "会失败的问题", task_id="failed-session-task"))

    assert result.status is TaskStatus.FAILED
    assert result.assistant_message_id is None
    assert [message["role"] for message in service.history(session["session_id"])] == ["user"]


def test_title_failure_keeps_default_title(runtime) -> None:
    service = _session_service(runtime, FakeModelBackend([AssistantTurn("回答")]))
    session = service.create()

    result = run(service.ask(session["session_id"], "标题失败", task_id="title-failure-task"))

    assert result.status is TaskStatus.SUCCESS
    assert result.title == "新会话"
    assert service.get(session["session_id"])["title"] == "新会话"


def test_successful_session_records_clean_git_commit_for_assistant(runtime) -> None:
    settings, _, _, _, _, _ = runtime
    _run_git(settings.project_root, "init")
    _run_git(settings.project_root, "config", "user.email", "test@example.com")
    _run_git(settings.project_root, "config", "user.name", "Session Test")
    (settings.project_root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _run_git(settings.project_root, "add", "tracked.txt")
    _run_git(settings.project_root, "commit", "-m", "initial")

    backend = FakeModelBackend([AssistantTurn("有版本回答"), AssistantTurn("版本标题")])
    service = _session_service(runtime, backend)
    session = service.create()
    result = run(service.ask(session["session_id"], "版本问题", task_id="git-session-task"))

    assert result.commit_sha is not None
    assert len(result.commit_sha) == 40
    assert service.commit_for_message(result.assistant_message_id)["commit_sha"] == result.commit_sha


def test_git_reader_uses_only_read_commands_and_rejects_dirty_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _run_git(repository, "init")
    _run_git(repository, "config", "user.email", "test@example.com")
    _run_git(repository, "config", "user.name", "Session Test")
    (repository / "README.md").write_text("clean\n", encoding="utf-8")
    _run_git(repository, "add", "README.md")
    _run_git(repository, "commit", "-m", "initial")

    calls: list[tuple[str, ...]] = []
    reader = GitReadOnly(repository)
    original = reader._run_readonly

    def record(arguments: tuple[str, ...]):
        calls.append(arguments)
        return original(arguments)

    reader._run_readonly = record
    clean = reader.read_clean_commit()

    assert clean.commit_sha is not None
    assert all(arguments[0] in {"rev-parse", "status"} for arguments in calls)
    assert all(arguments[0] not in {"add", "commit", "stash", "checkout", "reset", "push"} for arguments in calls)

    (repository / "README.md").write_text("dirty\n", encoding="utf-8")
    assert reader.read_clean_commit().commit_sha is None


def _run_git(repository: Path, *arguments: str) -> None:
    """测试仅在临时仓库建立提交，产品 GitReadOnly 不会调用这些写命令。"""

    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True, text=True)
