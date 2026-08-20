"""ContextBuilder 测试：验证活动分支、偏好和长期记忆的安全组装边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from likai_nexus.memory.context_builder import ContextBuilder, MemoryCandidate
from likai_nexus.memory.session import SessionService
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.agent_loop import AgentLoop
from likai_nexus.orchestrator.schemas import AssistantTurn
from likai_nexus.storage.commit_repository import CommitRepository
from likai_nexus.storage.database import Database
from likai_nexus.storage.memory_repository import MemoryRepository
from likai_nexus.storage.preference_repository import PreferenceRepository
from likai_nexus.storage.session_repository import SessionRepository


class FixedRetriever:
    """测试用检索器，验证 ContextBuilder 通过 ID 回 SQLite 读取正文。"""

    def __init__(self, candidates: tuple[MemoryCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, float, int]] = []

    def search(self, query: str, *, threshold: float, limit: int):
        self.calls.append((query, threshold, limit))
        return self.candidates


def _components(tmp_path: Path):
    database = Database(tmp_path / "context.sqlite3")
    database.initialize()
    sessions = SessionRepository(database)
    preferences = PreferenceRepository(database)
    memories = MemoryRepository(database)
    return database, sessions, preferences, memories


def test_context_builder_assembles_history_preferences_and_memory(tmp_path: Path) -> None:
    _, sessions, preferences, memories = _components(tmp_path)
    session = sessions.create()
    user = sessions.add_message(
        session["session_id"], "user", "项目应该怎样测试", parent_message_id=None
    )
    sessions.add_message(
        session["session_id"], "assistant", "使用 pytest", parent_message_id=user["message_id"]
    )
    preferences.set("language", "zh-CN")
    memory = memories.create("project", "项目使用 pytest", "user")
    retriever = FixedRetriever((MemoryCandidate(memory["memory_id"], 0.91),))
    builder = ContextBuilder(
        sessions,
        preferences,
        memories,
        retriever=retriever,
        memory_threshold=0.6,
        memory_limit=3,
    )

    result = builder.build(
        session["session_id"],
        "请总结项目测试方式",
        task_context={"task_id": "task-1", "status": "pending"},
    )

    assert result.history_count == 2
    assert result.preference_count == 1
    assert result.memory_count == 1
    assert retriever.calls == [("请总结项目测试方式", 0.6, 3)]
    assert result.messages[0].role == "system"
    assert "language=\"zh-CN\"" in result.messages[0].content
    assert "项目使用 pytest" in result.messages[0].content
    assert [message.content for message in result.messages[1:]] == [
        "项目应该怎样测试",
        "使用 pytest",
    ]


def test_sqlite_retriever_only_returns_similar_active_memory(tmp_path: Path) -> None:
    _, sessions, preferences, memories = _components(tmp_path)
    session = sessions.create()
    related = memories.create("lesson", "项目测试使用 pytest", "user")
    memories.create("fact", "用户喜欢简洁回答", "user")
    builder = ContextBuilder(
        sessions,
        preferences,
        memories,
        memory_threshold=0.45,
        memory_limit=5,
    )

    result = builder.build(session["session_id"], "请说明项目 pytest 测试方式")

    assert result.memory_count == 1
    assert related["content"] in result.messages[0].content


def test_session_service_uses_context_builder_before_current_request(runtime) -> None:
    _settings, database, tasks, _, approvals, executor = runtime
    sessions = SessionRepository(database)
    preferences = PreferenceRepository(database)
    memories = MemoryRepository(database)
    session = sessions.create()
    builder = ContextBuilder(sessions, preferences, memories)
    backend = FakeModelBackend([AssistantTurn("已读取上下文")])
    agent = AgentLoop(backend, executor, tasks, approvals=approvals, max_turns=3)
    service = SessionService(
        sessions,
        agent=agent,
        backend=backend,
        commits=CommitRepository(database),
        context_builder=builder,
    )

    service_result = asyncio.run(
        service.ask(session["session_id"], "验证上下文", task_id="context-task")
    )

    assert service_result.status.value == "success"
    assert [message.role for message in backend.messages[0]] == ["system", "system", "user"]
    assert "当前请求上下文说明" in backend.messages[0][1].content
    assert sum(message.content == "验证上下文" for message in backend.messages[0]) == 1
