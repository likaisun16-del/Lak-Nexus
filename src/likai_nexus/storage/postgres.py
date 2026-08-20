"""PostgreSQL 可选适配器：使用 DB-API 工厂，不在本地强制安装驱动或连接数据库。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from ..errors import MigrationError, NexusError, StorageError
from .postgres_schema import POSTGRES_SCHEMA_STATEMENTS, POSTGRES_TABLE_COLUMNS
from .snapshot import PORTABLE_TABLES, PortableSnapshot

_INSERT_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


class _PostgresRow(dict[str, Any]):
    """兼容现有仓储的 row[0] 与 row['column'] 两种读取方式。"""

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class _PostgresResult:
    """把 DB-API Cursor 的结果 materialize，避免游标越过事务边界。"""

    def __init__(self, cursor: Any) -> None:
        self.rowcount = cursor.rowcount
        description = cursor.description or ()
        columns = tuple(item[0] for item in description)
        raw_rows = cursor.fetchall() if description else ()
        self._rows = tuple(self._row(columns, row) for row in raw_rows)
        self._index = 0

    @staticmethod
    def _row(columns: tuple[str, ...], row: Any) -> _PostgresRow:
        if isinstance(row, Mapping):
            return _PostgresRow(row)
        return _PostgresRow(zip(columns, row))

    def fetchone(self) -> _PostgresRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[_PostgresRow]:
        rows = list(self._rows[self._index :])
        self._index = len(self._rows)
        return rows


class PostgresConnection:
    """提供现有 Repository 需要的 execute/fetchone/fetchall 最小接口。"""

    def __init__(self, raw_connection: Any) -> None:
        self.raw_connection = raw_connection

    def execute(self, statement: str, parameters: Sequence[object] = ()) -> _PostgresResult:
        cursor = self.raw_connection.cursor()
        try:
            cursor.execute(_translate_sql(statement), tuple(parameters))
            return _PostgresResult(cursor)
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                close()


class PostgresDatabase:
    """可注入 DB-API 连接工厂的 PostgreSQL 数据库门面。"""

    dialect = "postgresql"

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise StorageError("PostgreSQL 配置失败：connection_factory 必须可调用")
        self.connection_factory = connection_factory

    def initialize(self) -> None:
        """创建与当前 SQLite 业务表兼容的 PostgreSQL Schema。"""

        with self.connection() as connection:
            for statement in POSTGRES_SCHEMA_STATEMENTS:
                connection.execute(statement)

    @contextmanager
    def connection(self) -> Iterator[PostgresConnection]:
        """建立一笔事务；连接失败信息不包含 DSN、密码或 Token。"""

        try:
            raw_connection = self.connection_factory()
        except Exception as exc:
            raise StorageError(
                f"PostgreSQL 连接失败：无法创建连接，原因={type(exc).__name__}"
            ) from exc
        connection = PostgresConnection(raw_connection)
        try:
            yield connection
            raw_connection.commit()
        except NexusError:
            with suppress(Exception):
                raw_connection.rollback()
            raise
        except Exception as exc:
            with suppress(Exception):
                raw_connection.rollback()
            raise StorageError(
                f"PostgreSQL 事务失败：数据库操作未完成，原因={type(exc).__name__}"
            ) from exc
        finally:
            with suppress(Exception):
                raw_connection.close()


def restore_postgres_snapshot(
    database: PostgresDatabase, snapshot: PortableSnapshot
) -> dict[str, int]:
    """将 SQLite 快照导入空 PostgreSQL Schema，不覆盖已有记录。"""

    database.initialize()
    with database.connection() as connection:
        _ensure_postgres_empty(connection)
        _insert_snapshot_rows(connection, snapshot)
        _restore_message_links(connection, snapshot)
    return {table: len(snapshot.tables[table]) for table in PORTABLE_TABLES}


def _ensure_postgres_empty(connection: PostgresConnection) -> None:
    for table in PORTABLE_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise MigrationError(f"PostgreSQL 快照恢复失败：目标数据库非空：{table}")


def _insert_snapshot_rows(
    connection: PostgresConnection, snapshot: PortableSnapshot
) -> None:
    for table in PORTABLE_TABLES:
        rows = snapshot.tables[table]
        columns = POSTGRES_TABLE_COLUMNS[table]
        for index, row in enumerate(rows):
            unknown = set(row) - set(columns)
            if unknown:
                names = ", ".join(sorted(unknown))
                raise MigrationError(
                    f"PostgreSQL 快照恢复失败：表 {table} 第 {index} 行包含未知字段：{names}"
                )
            values = dict(row)
            if table == "sessions":
                values["active_leaf_id"] = None
            if table == "messages":
                values["parent_message_id"] = None
            ordered = [column for column in columns if column in values]
            placeholders = ", ".join("?" for _ in ordered)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(ordered)}) VALUES ({placeholders})",
                tuple(values[column] for column in ordered),
            )


def _restore_message_links(
    connection: PostgresConnection, snapshot: PortableSnapshot
) -> None:
    for row in snapshot.tables["messages"]:
        if row.get("message_id") is not None and row.get("parent_message_id") is not None:
            connection.execute(
                "UPDATE messages SET parent_message_id = ? WHERE message_id = ?",
                (row["parent_message_id"], row["message_id"]),
            )
    for row in snapshot.tables["sessions"]:
        if row.get("session_id") is not None and row.get("active_leaf_id") is not None:
            connection.execute(
                "UPDATE sessions SET active_leaf_id = ? WHERE session_id = ?",
                (row["active_leaf_id"], row["session_id"]),
            )


def _translate_sql(statement: str) -> str:
    """转换现有仓储使用的 SQLite 占位符和幂等插入语法。"""

    translated = statement
    if "FROM tool_calls" in translated:
        translated = translated.replace("ORDER BY rowid", "ORDER BY started_at, audit_id")
    if "FROM approvals" in translated:
        translated = translated.replace("ORDER BY rowid", "ORDER BY decided_at, approval_id")
    if "FROM messages" in translated or "FROM messages AS" in translated:
        translated = translated.replace("created_at, rowid", "created_at, message_id")
        translated = translated.replace("m.created_at, m.rowid", "m.created_at, m.message_id")
    if _INSERT_IGNORE.search(translated):
        translated = _INSERT_IGNORE.sub("INSERT INTO", translated, count=1).rstrip()
        translated = f"{translated} ON CONFLICT DO NOTHING"
    return translated.replace("?", "%s")
