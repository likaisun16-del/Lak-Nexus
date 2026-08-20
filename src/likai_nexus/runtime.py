"""运行时组装层：按配置连接 PostgreSQL 或 SQLite，避免 CLI 跨层组装存储依赖。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .errors import ConfigError, ToolExecutionError
from .events import EventSink
from .executor.service import ToolExecutor
from .git import GitReadOnly
from .memory.context_builder import ContextBuilder
from .memory.contracts import EmbeddingProvider, GraphMemoryAdapter
from .memory.postgres_vector import PostgresVectorIndex
from .memory.retrieval_adapters import VectorMemoryRetriever
from .memory.session import SessionService
from .models.base import ModelBackend
from .models.embedding import create_embedding_provider
from .models.openai_backend import OpenAICompatibleBackend
from .orchestrator.agent_loop import AgentLoop
from .safety.approval import ApprovalHandler, CliApprovalHandler
from .safety.review_mode import ReviewMode, parse_review_mode
from .storage.app_data import AppDataManager
from .storage.audit_repository import AuditRepository
from .storage.commit_repository import CommitRepository
from .storage.database import Database
from .storage.memory_repository import MemoryRepository
from .storage.postgres import PostgresDatabase
from .storage.preference_repository import PreferenceRepository
from .storage.preferences import DatabasePreferenceStore, migrate_legacy_preference_file
from .storage.session_repository import SessionRepository
from .storage.task_repository import TaskRepository
from .tools.builtin import build_builtin_tools
from .tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class Runtime:
    """CLI 需要的最小运行时对象，不暴露具体工具实现细节。"""

    agent: AgentLoop
    tasks: TaskRepository
    sessions: SessionService | None = None
    preferences: PreferenceRepository | None = None
    memories: MemoryRepository | None = None


def prepare_runtime(settings: Settings) -> tuple[str, ...]:
    """由运行时启动层准备应用目录，不自动迁移或读取 SQLite 数据库。"""

    return AppDataManager(
        settings.project_root,
        settings.workspace_root,
        settings.database_path,
        False,
    ).prepare()


def build_runtime(
    settings: Settings,
    *,
    backend: ModelBackend | None = None,
    approvals: ApprovalHandler | None = None,
    review_mode: ReviewMode = ReviewMode.STRICT,
    event_sink: EventSink | None = None,
    full_access_confirmed: bool = False,
    on_full_access_confirmed: Callable[[], None] | None = None,
) -> Runtime:
    """完成一次性任务所需的依赖组装，测试可注入 Fake Backend 和静态审批器。"""

    mode = parse_review_mode(review_mode)
    prepare_runtime(settings)
    approval_handler = approvals or CliApprovalHandler()
    registry = ToolRegistry(build_builtin_tools(settings, mode), settings.max_output_bytes, mode)
    if mode is not ReviewMode.FULL_ACCESS:
        try:
            registry.validate_runtime()
        except ToolExecutionError as exc:
            raise ConfigError(f"运行时配置失败：{exc}") from exc
    database = _build_database(settings)
    tasks = TaskRepository(database)  # type: ignore[arg-type]
    tasks.recover_running()
    preferences = PreferenceRepository(database)  # type: ignore[arg-type]
    memories = MemoryRepository(database)  # type: ignore[arg-type]
    audit = AuditRepository(database)  # type: ignore[arg-type]
    executor = ToolExecutor(
        registry, approval_handler, audit, mode, event_sink
    )
    model_backend = backend or OpenAICompatibleBackend(settings)
    agent = AgentLoop(
        model_backend,
        executor,
        tasks,
        settings.max_turns,
        review_mode=mode,
        approvals=approval_handler,
        event_sink=event_sink,
        full_access_confirmed=full_access_confirmed,
        on_full_access_confirmed=on_full_access_confirmed,
    )
    session_repository = SessionRepository(database)  # type: ignore[arg-type]
    context_builder = _build_context_builder(
        settings, database, session_repository, preferences, memories
    )
    sessions = SessionService(
        session_repository,
        agent=agent,
        backend=model_backend,
        commits=CommitRepository(database),  # type: ignore[arg-type]
        git_reader=GitReadOnly(settings.project_root),
        context_builder=context_builder,
    )
    return Runtime(
        agent=agent,
        tasks=tasks,
        sessions=sessions,
        preferences=preferences,
        memories=memories,
    )


def build_session_service(settings: Settings) -> SessionService:
    """只组装 Session 存储，供不需要模型调用的会话管理命令使用。"""

    prepare_runtime(settings)
    database = _build_database(settings)
    return SessionService(
        SessionRepository(database),  # type: ignore[arg-type]
        commits=CommitRepository(database),  # type: ignore[arg-type]
        git_reader=GitReadOnly(settings.project_root),
    )


def build_memory_repository(settings: Settings) -> MemoryRepository:
    """组装无需模型调用的长期记忆仓储，供 CLI 管理命令使用。"""

    prepare_runtime(settings)
    return MemoryRepository(_build_database(settings))  # type: ignore[arg-type]


def build_preference_store(
    settings: Settings,
) -> tuple[DatabasePreferenceStore, tuple[str, ...]]:
    """组装数据库偏好存储，并执行一次旧 JSON 偏好迁移。"""

    prepare_runtime(settings)
    database = _build_database(settings)
    repository = PreferenceRepository(database)  # type: ignore[arg-type]
    notices = migrate_legacy_preference_file(repository, settings.preference_path)
    return DatabasePreferenceStore(repository), notices


def _build_database(settings: Settings) -> Database | PostgresDatabase:
    """按配置初始化主存储；默认 PG 启动不再读取或迁移 SQLite 文件。"""

    if settings.storage_backend == "sqlite":
        database = Database(settings.database_path)
        database.initialize()
        return database

    database = PostgresDatabase(_postgres_connection_factory(settings.postgres_dsn))
    database.initialize()
    return database


def _postgres_connection_factory(dsn: str) -> Callable[[], object]:
    """延迟加载 psycopg，保持 SQLite 显式回退不依赖 PostgreSQL 驱动。"""

    try:
        import psycopg
    except ImportError as exc:
        raise ConfigError(
            "PostgreSQL 配置失败：缺少 psycopg 驱动，请安装项目的 postgres 依赖"
        ) from exc

    return lambda: psycopg.connect(dsn)


def _build_context_builder(
    settings: Settings,
    database: Database | PostgresDatabase,
    sessions: SessionRepository,
    preferences: PreferenceRepository,
    memories: MemoryRepository,
) -> ContextBuilder:
    """组装当前存储的上下文检索；只有 PG 后端启用 pgvector Provider。"""

    if not isinstance(database, PostgresDatabase):
        if settings.embedding_provider not in {"", "none", "disabled"}:
            raise ConfigError(
                "Embedding 配置失败：EMBEDDING_PROVIDER 只有在 STORAGE_BACKEND=postgres 时可用"
            )
        return ContextBuilder(sessions, preferences, memories)

    embedding_provider = create_embedding_provider(settings)
    retriever = None
    if embedding_provider is not None:
        vector_index = PostgresVectorIndex(database, embedding_provider.dimension)
        vector_index.initialize()
        retriever = VectorMemoryRetriever(embedding_provider, vector_index)
    return ContextBuilder(
        sessions,
        preferences,
        memories,
        retriever=retriever,
    )


def build_postgres_context_builder(
    connection_factory: Callable[[], object],
    *,
    embedding_provider: EmbeddingProvider | None = None,
    graph_adapter: GraphMemoryAdapter | None = None,
) -> tuple[PostgresDatabase, ContextBuilder]:
    """组装可选 PostgreSQL/pgvector 上下文，默认 SQLite Runtime 不受影响。"""

    database = PostgresDatabase(connection_factory)
    database.initialize()
    sessions = SessionRepository(database)  # type: ignore[arg-type]
    preferences = PreferenceRepository(database)  # type: ignore[arg-type]
    memories = MemoryRepository(database)  # type: ignore[arg-type]
    retriever = None
    if embedding_provider is not None:
        vector_index = PostgresVectorIndex(database, embedding_provider.dimension)
        vector_index.initialize()
        retriever = VectorMemoryRetriever(embedding_provider, vector_index)
    return database, ContextBuilder(
        sessions,
        preferences,
        memories,
        retriever=retriever,
        graph_adapter=graph_adapter,
    )
