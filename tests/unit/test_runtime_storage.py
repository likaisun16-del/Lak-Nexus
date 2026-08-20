"""运行时存储选择测试：验证 PostgreSQL 默认值和 SQLite 显式回退路径。"""

from __future__ import annotations

from pathlib import Path

import likai_nexus.runtime as runtime_module
from likai_nexus.config import Settings
from likai_nexus.storage.database import Database
from likai_nexus.storage.postgres import PostgresDatabase


class FakePostgresDatabase:
    """隔离运行时后端选择测试的 PostgreSQL 门面替身。"""

    dialect = "postgresql"

    def __init__(self, connection_factory) -> None:
        self.connection_factory = connection_factory
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def is_empty(self) -> bool:
        return True


def test_runtime_uses_sqlite_only_when_explicitly_configured(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        project_root=tmp_path,
        database_path=tmp_path.parent / "sqlite-fallback.db",
        storage_backend="sqlite",
    )

    database = runtime_module._build_database(settings)

    assert isinstance(database, Database)
    assert not isinstance(database, PostgresDatabase)


def test_runtime_defaults_to_postgres_backend(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        project_root=tmp_path,
        database_path=tmp_path.parent / "postgres-source.db",
    )
    fake = FakePostgresDatabase(lambda: None)
    monkeypatch.setattr(runtime_module, "PostgresDatabase", lambda factory: fake)
    monkeypatch.setattr(
        runtime_module,
        "_postgres_connection_factory",
        lambda dsn: (lambda: None),
    )

    database = runtime_module._build_database(settings)

    assert database is fake
    assert fake.initialized is True
