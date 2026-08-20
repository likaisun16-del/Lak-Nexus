"""SQLite 记忆存储测试：验证偏好、长期记忆、迁移、去重和安全边界。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from likai_nexus.errors import PreferenceError, ValidationError
from likai_nexus.storage.database import Database
from likai_nexus.storage.memory_repository import MemoryRepository
from likai_nexus.storage.preference_repository import PreferenceRepository
from likai_nexus.storage.task_repository import TaskRepository


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    return database


def test_database_initializes_memory_tables_and_constraints(tmp_path: Path) -> None:
    database = _database(tmp_path)

    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"preferences", "memories"}.issubset(tables)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memories(memory_id, memory_type, content, source_type, "
                "importance, content_hash, created_at, updated_at) "
                "VALUES ('bad', 'unknown', 'x', 'user', 0.5, 'hash', 'now', 'now')"
            )


def test_preference_repository_round_trip_override_and_system_guard(tmp_path: Path) -> None:
    repository = PreferenceRepository(_database(tmp_path))

    repository.set("language", "zh-CN")
    assert repository.get("language") == "zh-CN"
    repository.set("language", "en-US")
    assert repository.get_record("language")["source"] == "user"
    with pytest.raises(PreferenceError, match="不能覆盖"):
        repository.set("language", "system-default", source="system")
    repository.set("answer_style", {"short": True}, source="system")
    assert repository.get("answer_style") == {"short": True}


def test_preference_repository_corrupt_json_returns_default(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = PreferenceRepository(database)
    repository.set("language", "zh-CN")
    with database.connection() as connection:
        connection.execute(
            "UPDATE preferences SET value_json = ? WHERE preference_key = ?",
            ("{broken", "language"),
        )

    assert repository.get("language", "strict") == "strict"
    assert repository.get_record("language") is None


def test_preference_repository_rejects_sensitive_values(tmp_path: Path) -> None:
    repository = PreferenceRepository(_database(tmp_path))

    with pytest.raises(PreferenceError, match="敏感字段"):
        repository.set("config", {"api_token": "secret"})
    with pytest.raises(PreferenceError, match="Token"):
        repository.set("config", "Bearer abcdefgh")


def test_memory_repository_create_deduplicates_and_lists_active(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))

    first = repository.create("fact", "用户偏好简洁中文回答", "user", importance=0.8)
    duplicate = repository.create("fact", "用户偏好简洁中文回答", "user")

    assert duplicate["memory_id"] == first["memory_id"]
    assert len(repository.list_active()) == 1
    assert first["embedding_status"] == "pending"


def test_memory_repository_validates_sources_and_can_disable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = MemoryRepository(database)
    tasks = TaskRepository(database)

    with pytest.raises(ValidationError, match="必须提供 source_ref"):
        repository.create("fact", "任务来源记忆", "task")
    with pytest.raises(ValidationError, match="来源不存在"):
        repository.create("fact", "任务来源记忆", "task", "missing-task")

    tasks.create("task-1", "测试任务")
    memory = repository.create("lesson", "任务完成后需要运行测试", "task", "task-1")
    assert repository.find_by_source("task", "task-1")[0]["memory_id"] == memory["memory_id"]
    assert repository.disable(memory["memory_id"])
    assert repository.list_active() == []
    assert not repository.disable(memory["memory_id"])


def test_memory_update_resets_embedding_and_rejects_duplicate(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    first = repository.create("project", "项目使用 SQLite", "user")
    second = repository.create("project", "项目使用 PostgreSQL", "user")
    repository.set_embedding_status(first["memory_id"], "ready")

    updated = repository.update(first["memory_id"], content="项目先使用 SQLite 验证")
    assert updated["embedding_status"] == "pending"
    with pytest.raises(ValidationError, match="重复"):
        repository.update(second["memory_id"], content="项目先使用 SQLite 验证")


def test_memory_repository_rejects_sensitive_content_and_invalid_importance(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))

    with pytest.raises(ValidationError, match="Token"):
        repository.create("fact", "OPENAI_API_KEY=sk-test-value", "user")
    with pytest.raises(ValidationError, match="范围"):
        repository.create("fact", "普通记忆", "user", importance=1.1)


def test_memory_repository_stores_json_like_text_without_modifying_content(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    content = json.dumps({"rule": "只保存用户明确要求记住的内容"}, ensure_ascii=False)

    memory = repository.create("lesson", content, "system")

    assert memory["content"] == content
