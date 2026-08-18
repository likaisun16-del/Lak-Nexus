"""SQLite 基础设施：为任务、ToolExecutor 和审计仓储创建表，并提供带事务的连接上下文。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Database:
    """SQLite 数据库封装，仓储层通过它完成参数化 SQL 访问。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """创建任务、工具调用和审批表。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            self._migrate_tool_calls(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    request_text TEXT NOT NULL,
                    review_mode TEXT NOT NULL DEFAULT 'strict',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_summary TEXT,
                    error_type TEXT,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    audit_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    tool_name TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    arguments_redacted TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_summary TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    UNIQUE(task_id, tool_call_id)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    tool_call_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    request_summary TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decision_source TEXT NOT NULL DEFAULT 'legacy',
                    decided_at TEXT NOT NULL
                );
                """
            )
            self._migrate_tasks(connection)
            self._migrate_approvals(connection)

    @staticmethod
    def _migrate_tasks(connection: sqlite3.Connection) -> None:
        """为旧任务补充审查模式，旧记录按 strict 解释且不覆盖原数据。"""

        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        if columns and "review_mode" not in columns:
            connection.execute(
                "ALTER TABLE tasks ADD COLUMN review_mode TEXT NOT NULL DEFAULT 'strict'"
            )

    @staticmethod
    def _migrate_approvals(connection: sqlite3.Connection) -> None:
        """为旧审批记录补充来源字段，保留既有人工决定和摘要。"""

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        if columns and "decision_source" not in columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN decision_source TEXT NOT NULL DEFAULT 'legacy'"
            )

    @staticmethod
    def _migrate_tool_calls(connection: sqlite3.Connection) -> None:
        """把旧版供应商调用 ID 主键迁移为内部审计主键加任务内唯一约束。"""

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        if not columns or "audit_id" in columns:
            return
        connection.execute("ALTER TABLE tool_calls RENAME TO tool_calls_legacy")
        connection.executescript(
            """
            CREATE TABLE tool_calls (
                audit_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                tool_name TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                arguments_redacted TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result_summary TEXT,
                error_type TEXT,
                error_message TEXT,
                UNIQUE(task_id, tool_call_id)
            );
            """
        )
        rows = connection.execute("SELECT * FROM tool_calls_legacy").fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO tool_calls(
                    audit_id, task_id, tool_name, tool_call_id, arguments_redacted,
                    status, started_at, finished_at, result_summary, error_type, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    row[1],
                    row[2],
                    row[0],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                ),
            )
        connection.execute("DROP TABLE tool_calls_legacy")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """提供自动提交/回滚的数据库连接，避免遗漏事务边界。"""

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
