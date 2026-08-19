"""偏好和应用目录测试：验证模式持久化、脚本目录初始化及旧数据库安全迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from likai_nexus.config import Settings
from likai_nexus.errors import ConfigError, PathAccessError
from likai_nexus.executor.tools import build_builtin_tools
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.preferences import LocalPreferenceStore


def _create_database(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def test_preferences_round_trip_and_corruption_falls_back_to_strict(tmp_path: Path) -> None:
    store = LocalPreferenceStore(tmp_path / "data" / "preferences.json")

    assert store.load_review_mode().mode is None
    store.save_review_mode(ReviewMode.FULL_ACCESS)
    assert store.load_review_mode().mode is ReviewMode.FULL_ACCESS

    store.path.write_text("{broken", encoding="utf-8")
    result = store.load_review_mode()
    assert result.mode is None
    assert result.warning is not None
    assert "strict" in result.warning


def test_runtime_prepares_data_and_script_directories_idempotently(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        workspace_root=workspace,
        project_root=tmp_path,
    )

    settings.prepare_runtime()
    settings.prepare_runtime()

    assert settings.data_root.is_dir()
    assert settings.script_root.is_dir()
    assert settings.database_path == tmp_path / "data" / "likai_nexus.db"


def test_legacy_workspace_database_is_migrated_and_backed_up(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / "data" / "likai_nexus.db"
    _create_database(legacy, "legacy")
    settings = Settings(
        workspace_root=workspace,
        project_root=tmp_path,
        database_path=Path("data/likai_nexus.db"),
    )

    notices = settings.prepare_runtime()

    assert settings.database_path.is_file()
    assert not legacy.exists()
    assert "旧数据库已迁移" in notices[0]
    backups = list((tmp_path / "data").glob("legacy-backup-*"))
    assert len(backups) == 1
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "legacy"
    assert (backups[0] / "likai_nexus.db").is_file()


def test_legacy_database_conflict_keeps_secondary_copy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "data" / "likai_nexus.db"
    second = workspace / ".likai_nexus" / "tasks.db"
    _create_database(first, "first")
    _create_database(second, "second")
    settings = Settings(workspace_root=workspace, project_root=tmp_path)

    notices = settings.prepare_runtime()

    assert "多个旧数据库" in notices[0]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "first"
    assert not first.exists()
    assert not second.exists()
    backup_files = list((tmp_path / "data").glob("legacy-backup-*/*"))
    assert {path.name for path in backup_files} == {"likai_nexus.db", "tasks.db"}


def test_invalid_legacy_database_stops_without_removing_original(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".likai_nexus" / "tasks.db"
    legacy.parent.mkdir()
    legacy.write_text("not sqlite", encoding="utf-8")
    settings = Settings(workspace_root=workspace, project_root=tmp_path)

    with pytest.raises(ConfigError, match="数据库校验失败"):
        settings.prepare_runtime()

    assert legacy.exists()
    assert not settings.database_path.exists()


def test_strict_and_relaxed_tools_cannot_read_project_data(tmp_path: Path) -> None:
    workspace = tmp_path
    data_file = tmp_path / "data" / "private.db"
    data_file.parent.mkdir()
    data_file.write_text("application data", encoding="utf-8")

    for mode in (ReviewMode.STRICT, ReviewMode.RELAXED):
        settings = Settings(
            workspace_root=workspace,
            project_root=tmp_path,
            database_path=tmp_path.parent / f"{tmp_path.name}-{mode.value}.sqlite3",
        )
        read_tool = build_builtin_tools(settings, mode)[0]
        with pytest.raises(PathAccessError, match="应用数据目录"):
            read_tool.check_safety(
                {"path": "data/private.db", "offset": 0, "byte_offset": 0, "limit": 1}
            )

    full_settings = Settings(
        workspace_root=workspace,
        project_root=tmp_path,
        database_path=tmp_path.parent / f"{tmp_path.name}-full.sqlite3",
    )
    full_read_tool = build_builtin_tools(full_settings, ReviewMode.FULL_ACCESS)[0]
    full_read_tool.check_safety(
        {"path": str(data_file), "offset": 0, "byte_offset": 0, "limit": 1}
    )
