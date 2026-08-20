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
        """创建任务、工具调用、审批、Session 和 Git 关联表。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            self._migrate_tool_calls(connection)
            self._prepare_legacy_messages(connection)
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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '新会话',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL,
                    active_leaf_id TEXT REFERENCES messages(message_id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    parent_message_id TEXT REFERENCES messages(message_id) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                    execution_status TEXT,
                    retry_of_message_id TEXT REFERENCES messages(message_id) ON DELETE SET NULL,
                    version_reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_commits (
                    association_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
                    commit_sha TEXT NOT NULL,
                    repository_path TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at, message_id);
                CREATE INDEX IF NOT EXISTS idx_messages_parent
                    ON messages(parent_message_id);
                CREATE INDEX IF NOT EXISTS idx_task_commits_task
                    ON task_commits(task_id);
                """
            )
            self._migrate_tasks(connection)
            self._migrate_approvals(connection)
            self._migrate_session_columns(connection)
            self._migrate_message_columns(connection)
            self._migrate_legacy_message_rows(connection)

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
    def _migrate_session_columns(connection: sqlite3.Connection) -> None:
        """为旧会话补充独立的最近消息时间，避免标题或分支指针更新冒充新消息。"""

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if columns and "last_message_at" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN last_message_at TEXT")
            connection.execute(
                "UPDATE sessions SET last_message_at = COALESCE(last_message_at, updated_at)"
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

    @staticmethod
    def _prepare_legacy_messages(connection: sqlite3.Connection) -> None:
        """把旧版线性消息表改名，待新树形表创建后按原顺序迁移。"""

        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
        ).fetchone()
        if table is None:
            return
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if {"parent_message_id", "task_id"}.issubset(columns):
            return
        if not {"session_id", "role", "content"}.issubset(columns):
            raise RuntimeError("数据库迁移失败：旧 messages 表缺少 session_id、role 或 content")
        connection.execute("ALTER TABLE messages RENAME TO messages_linear_legacy")

    @staticmethod
    def _migrate_message_columns(connection: sqlite3.Connection) -> None:
        """为已存在的树形消息表补充可选关联列，保持旧数据可读。"""

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if columns and "parent_message_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN parent_message_id TEXT")
        if columns and "task_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN task_id TEXT")
        if columns and "execution_status" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN execution_status TEXT")
        if columns and "retry_of_message_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN retry_of_message_id TEXT")
        if columns and "version_reason" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN version_reason TEXT")

    @staticmethod
    def _migrate_legacy_message_rows(connection: sqlite3.Connection) -> None:
        """将旧线性消息按会话和原 rowid 重建为父子链，不删除原内容。"""

        legacy = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages_linear_legacy'"
        ).fetchone()
        if legacy is None:
            return
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(messages_linear_legacy)").fetchall()
        }
        message_id_column = "message_id" if "message_id" in columns else "id"
        if message_id_column not in columns:
            message_id_column = None
        task_id_expression = "task_id" if "task_id" in columns else "NULL"
        created_at_expression = "created_at" if "created_at" in columns else "NULL"
        rows = connection.execute(
            f"SELECT rowid, {message_id_column or 'NULL'}, session_id, role, content, "
            f"{task_id_expression}, {created_at_expression} "
            "FROM messages_linear_legacy ORDER BY session_id, rowid"
        ).fetchall()
        previous_by_session: dict[str, str] = {}
        session_times: dict[str, list[str]] = {}
        for row in rows:
            session_id = str(row[2])
            created_at = row[6] or utc_now_fallback()
            session_times.setdefault(session_id, []).append(created_at)
            connection.execute(
                "INSERT OR IGNORE INTO sessions(session_id, title, created_at, updated_at, last_message_at) "
                "VALUES (?, '新会话', ?, ?, ?)",
                (session_id, created_at, created_at, created_at),
            )
            message_id = str(row[1]) if row[1] else uuid.uuid4().hex
            parent_id = previous_by_session.get(session_id)
            connection.execute(
                "INSERT OR IGNORE INTO messages(message_id, session_id, role, content, "
                "parent_message_id, task_id, execution_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    row[3],
                    row[4],
                    parent_id,
                    row[5],
                    "success" if row[3] == "assistant" else None,
                    created_at,
                ),
            )
            previous_by_session[session_id] = message_id
            connection.execute(
                "UPDATE sessions SET active_leaf_id = ?, updated_at = ?, last_message_at = ? "
                "WHERE session_id = ?",
                (message_id, created_at, created_at, session_id),
            )
        connection.execute("DROP TABLE messages_linear_legacy")

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


def utc_now_fallback() -> str:
    """迁移缺少时间字段时提供可排序的 UTC 时间。"""

    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")
