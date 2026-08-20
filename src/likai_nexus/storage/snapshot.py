"""SQLite 可移植快照：为未来 PostgreSQL 迁移提供受校验的数据交换格式。"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..errors import MigrationError
from .database import Database

SNAPSHOT_FORMAT = "likai-nexus.sqlite-snapshot"
SNAPSHOT_SCHEMA_VERSION = 1
PORTABLE_TABLES = (
    "tasks",
    "preferences",
    "memories",
    "sessions",
    "messages",
    "tool_calls",
    "approvals",
    "task_commits",
)
_IMPORT_ORDER = (
    "tasks",
    "preferences",
    "memories",
    "sessions",
    "messages",
    "tool_calls",
    "approvals",
    "task_commits",
)
_BLOB_MARKER = "__likai_nexus_blob__"


@dataclass(frozen=True, slots=True)
class PortableSnapshot:
    """不包含数据库连接细节的快照对象，可供未来 PostgreSQL 导入器消费。"""

    schema_version: int
    tables: dict[str, tuple[dict[str, object], ...]]

    def to_dict(self) -> dict[str, object]:
        """转换为可 JSON 序列化的稳定结构。"""

        return {
            "format": SNAPSHOT_FORMAT,
            "schema_version": self.schema_version,
            "tables": {
                table: [
                    {key: _encode_value(value) for key, value in row.items()}
                    for row in rows
                ]
                for table, rows in self.tables.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: object) -> PortableSnapshot:
        """严格解析快照，拒绝未知表、未知字段容器和非法 JSON 类型。"""

        if not isinstance(payload, dict) or payload.get("format") != SNAPSHOT_FORMAT:
            raise MigrationError("快照读取失败：format 不匹配")
        version = payload.get("schema_version")
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise MigrationError(f"快照读取失败：不支持的 schema_version：{version}")
        raw_tables = payload.get("tables")
        if not isinstance(raw_tables, dict):
            raise MigrationError("快照读取失败：tables 必须是对象")
        unknown_tables = set(raw_tables) - set(PORTABLE_TABLES)
        if unknown_tables:
            names = ", ".join(sorted(str(name) for name in unknown_tables))
            raise MigrationError(f"快照读取失败：包含未知表：{names}")
        tables: dict[str, tuple[dict[str, object], ...]] = {}
        for table in PORTABLE_TABLES:
            raw_rows = raw_tables.get(table, [])
            if not isinstance(raw_rows, list):
                raise MigrationError(f"快照读取失败：表 {table} 必须是数组")
            rows: list[dict[str, object]] = []
            for index, raw_row in enumerate(raw_rows):
                if not isinstance(raw_row, dict) or any(
                    not isinstance(key, str) for key in raw_row
                ):
                    raise MigrationError(f"快照读取失败：表 {table} 第 {index} 行格式无效")
                rows.append({key: _decode_value(value) for key, value in raw_row.items()})
            tables[table] = tuple(rows)
        return cls(schema_version=version, tables=tables)

    def write_json(self, path: Path) -> None:
        """写出快照文件；不把表内容打印到终端。"""

        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise MigrationError(
                f"快照写入失败：目标={target}，原因={type(exc).__name__}"
            ) from exc

    @classmethod
    def read_json(cls, path: Path) -> PortableSnapshot:
        """读取并校验 JSON 快照文件。"""

        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"快照读取失败：目标={source}，原因={type(exc).__name__}"
            ) from exc
        return cls.from_dict(payload)


class SQLiteSnapshotExporter:
    """从当前 SQLite 权威库生成可迁移快照，不修改源数据内容。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def export(self) -> PortableSnapshot:
        """先确保 SQLite 完成内置迁移，再按固定表白名单读取数据。"""

        if not self.database.path.exists():
            raise MigrationError(f"快照导出失败：源数据库不存在：{self.database.path}")
        self.database.initialize()
        with self.database.connection() as connection:
            tables = {
                table: self._read_table(connection, table) for table in PORTABLE_TABLES
            }
        return PortableSnapshot(SNAPSHOT_SCHEMA_VERSION, tables)

    @staticmethod
    def _read_table(
        connection: sqlite3.Connection, table: str
    ) -> tuple[dict[str, object], ...]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is None:
            raise MigrationError(f"快照导出失败：源数据库缺少表：{table}")
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        column_names = tuple(rows[0].keys()) if rows else ()
        return tuple(
            {key: value for key, value in zip(column_names, row, strict=True)} for row in rows
        )


def restore_sqlite_snapshot(database: Database, snapshot: PortableSnapshot) -> dict[str, int]:
    """将快照恢复到空 SQLite 数据库，拒绝覆盖已有数据。"""

    if snapshot.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise MigrationError(
            f"快照恢复失败：不支持的 schema_version：{snapshot.schema_version}"
        )
    database.initialize()
    with database.connection() as connection:
        _ensure_empty(connection)
        _insert_snapshot_rows(connection, snapshot)
        _restore_message_links(connection, snapshot)
    return {table: len(snapshot.tables[table]) for table in PORTABLE_TABLES}


def _ensure_empty(connection: sqlite3.Connection) -> None:
    for table in PORTABLE_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise MigrationError(f"快照恢复失败：目标数据库非空：{table}")


def _insert_snapshot_rows(connection: sqlite3.Connection, snapshot: PortableSnapshot) -> None:
    for table in _IMPORT_ORDER:
        rows = snapshot.tables[table]
        if not rows:
            continue
        columns = _table_columns(connection, table)
        for index, row in enumerate(rows):
            unknown = set(row) - set(columns)
            if unknown:
                names = ", ".join(sorted(unknown))
                raise MigrationError(f"快照恢复失败：表 {table} 第 {index} 行包含未知字段：{names}")
            values = {key: _decode_value(value) for key, value in row.items()}
            if table == "sessions":
                values["active_leaf_id"] = None
            if table == "messages":
                values["parent_message_id"] = None
            ordered = [key for key in columns if key in values]
            placeholders = ", ".join("?" for _ in ordered)
            try:
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(ordered)}) VALUES ({placeholders})",
                    tuple(values[key] for key in ordered),
                )
            except sqlite3.DatabaseError as exc:
                raise MigrationError(
                    f"快照恢复失败：表 {table} 第 {index} 行写入失败：{type(exc).__name__}"
                ) from exc


def _restore_message_links(connection: sqlite3.Connection, snapshot: PortableSnapshot) -> None:
    for row in snapshot.tables["messages"]:
        message_id = row.get("message_id")
        parent_id = row.get("parent_message_id")
        if message_id is not None and parent_id is not None:
            connection.execute(
                "UPDATE messages SET parent_message_id = ? WHERE message_id = ?",
                (_decode_value(parent_id), _decode_value(message_id)),
            )
    for row in snapshot.tables["sessions"]:
        session_id = row.get("session_id")
        leaf_id = row.get("active_leaf_id")
        if session_id is not None and leaf_id is not None:
            connection.execute(
                "UPDATE sessions SET active_leaf_id = ? WHERE session_id = ?",
                (_decode_value(leaf_id), _decode_value(session_id)),
            )


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def _encode_value(value: object) -> object:
    if isinstance(value, bytes):
        return {_BLOB_MARKER: base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise MigrationError(f"快照编码失败：不支持的 SQLite 值类型：{type(value).__name__}")


def _decode_value(value: object) -> object:
    if not isinstance(value, dict) or _BLOB_MARKER not in value:
        return value
    encoded = value.get(_BLOB_MARKER)
    if not isinstance(encoded, str):
        raise MigrationError("快照解码失败：BLOB 标记内容无效")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise MigrationError("快照解码失败：BLOB 内容不是有效 Base64") from exc
