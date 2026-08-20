"""PostgreSQL 主存储 Schema：与当前 SQLite 事实表保持同一业务字段。"""

from __future__ import annotations

POSTGRES_SCHEMA_STATEMENTS = (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS preferences (
        preference_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('user', 'system')),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id TEXT PRIMARY KEY,
        memory_type TEXT NOT NULL CHECK (memory_type IN ('fact', 'project', 'lesson')),
        content TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK (
            source_type IN ('user', 'conversation', 'task', 'system')
        ),
        source_ref TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
        importance DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (
            importance >= 0.0 AND importance <= 1.0
        ),
        content_hash TEXT NOT NULL,
        embedding_status TEXT NOT NULL DEFAULT 'pending' CHECK (
            embedding_status IN ('pending', 'ready', 'failed')
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '新会话',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_message_at TEXT NOT NULL,
        active_leaf_id TEXT
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        tool_call_id TEXT NOT NULL,
        action_type TEXT NOT NULL,
        request_summary TEXT NOT NULL,
        decision TEXT NOT NULL,
        decision_source TEXT NOT NULL DEFAULT 'legacy',
        decided_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_commits (
        association_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
        commit_sha TEXT NOT NULL,
        repository_path TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_commits_task ON task_commits(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_type, source_ref)",
    "CREATE INDEX IF NOT EXISTS idx_memories_status_updated ON memories(status, updated_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_hash ON memories(content_hash) WHERE status = 'active'",
)

POSTGRES_TABLE_COLUMNS = {
    "tasks": (
        "task_id", "request_text", "review_mode", "status", "created_at", "started_at",
        "finished_at", "result_summary", "error_type", "error_message",
    ),
    "preferences": ("preference_key", "value_json", "source", "updated_at"),
    "memories": (
        "memory_id", "memory_type", "content", "source_type", "source_ref", "status",
        "importance", "content_hash", "embedding_status", "created_at", "updated_at",
    ),
    "sessions": ("session_id", "title", "created_at", "updated_at", "last_message_at", "active_leaf_id"),
    "messages": (
        "message_id", "session_id", "role", "content", "parent_message_id", "task_id",
        "execution_status", "retry_of_message_id", "version_reason", "created_at",
    ),
    "tool_calls": (
        "audit_id", "task_id", "tool_name", "tool_call_id", "arguments_redacted", "status",
        "started_at", "finished_at", "result_summary", "error_type", "error_message",
    ),
    "approvals": (
        "approval_id", "task_id", "tool_call_id", "action_type", "request_summary", "decision",
        "decision_source", "decided_at",
    ),
    "task_commits": (
        "association_id", "task_id", "commit_sha", "repository_path", "created_at",
    ),
}
