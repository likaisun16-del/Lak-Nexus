"""Session 领域服务：组织当前分支问答、标题生成和 Task 的只读 Git 关联。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from ..errors import SessionError, TaskAlreadyExistsError
from ..git import GitCommitSnapshot, GitReadOnly
from ..models.base import ModelBackend
from ..orchestrator.agent_loop import AgentLoop
from ..orchestrator.schemas import AgentResult, ChatMessage, TaskStatus
from ..safety.redaction import redact_text, truncate_text
from ..storage.commit_repository import CommitRepository
from ..storage.session_repository import DEFAULT_SESSION_TITLE, SessionRepository
from .context_builder import ContextBuilder

_TITLE_SYSTEM_PROMPT = (
    "请根据下面首轮用户问题和最终回答生成一个简短中文会话标题。"
    "只输出标题本身，不要解释，不要使用 Markdown，不超过 20 个汉字。"
)


@dataclass(frozen=True, slots=True)
class SessionAskResult:
    """一次 Session 问答的可见结果与关联标识。"""

    session_id: str
    task_id: str
    result: AgentResult
    user_message_id: str
    assistant_message_id: str | None = None
    commit_sha: str | None = None
    title: str = DEFAULT_SESSION_TITLE
    commit_reason: str | None = None

    @property
    def status(self) -> TaskStatus:
        return self.result.status

    @property
    def content(self) -> str:
        return self.result.content

    @property
    def error_message(self) -> str | None:
        return self.result.error_message

    @property
    def turns(self) -> int:
        return self.result.turns


class SessionService:
    """会话树的业务入口，不让 CLI 直接决定消息父子关系或模型上下文。"""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        agent: AgentLoop | None = None,
        backend: ModelBackend | None = None,
        commits: CommitRepository | None = None,
        git_reader: GitReadOnly | None = None,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.repository = repository
        self.agent = agent
        self.backend = backend
        self.commits = commits
        self.git_reader = git_reader
        self.context_builder = context_builder

    def create(self, title: str = DEFAULT_SESSION_TITLE) -> dict[str, Any]:
        """创建会话并返回稳定标识。"""

        return self.repository.create(title=title)

    def get(self, session_id: str) -> dict[str, Any] | None:
        """读取会话元数据。"""

        return self.repository.get(session_id)

    def list(self) -> list[dict[str, Any]]:
        """列出会话，排序由仓储层统一保证。"""

        return self.repository.list()

    def history(self, session_id: str) -> list[dict[str, Any]]:
        """读取活动分支从根到叶子的可见消息。"""

        return self.repository.current_path(session_id)

    def branches(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        """读取分支点和叶子摘要。"""

        return self.repository.list_branches(session_id)

    def continue_from(self, session_id: str, message_id: str) -> None:
        """只在调用方当前 Session 内切换活动叶子，拒绝跨会话副作用。"""

        if self.repository.get(session_id) is None:
            raise SessionError(f"继续会话失败：当前 Session 不存在：{session_id}")
        message_session_id = self.repository.get_session_for_message(message_id)
        if message_session_id is None:
            raise SessionError(f"继续会话失败：消息不存在：{message_id}")
        if message_session_id != session_id:
            raise SessionError(
                f"继续会话失败：消息属于其他 Session，未切换当前活动分支：{message_id}"
            )
        self.repository.set_active_leaf(session_id, message_id)

    def switch(self, session_id: str) -> dict[str, Any]:
        """校验并返回可切换的 Session。"""

        session = self.repository.get(session_id)
        if session is None:
            raise ValueError(f"会话切换失败：Session 不存在：{session_id}")
        return session

    def delete(self, session_id: str) -> bool:
        """删除会话树，保留 Task、工具、审批和 Commit 记录。"""

        return self.repository.delete(session_id)

    def commit_for_message(self, message_id: str) -> dict[str, Any] | None:
        """查询 assistant 消息对应的 Commit，未记录时返回 None。"""

        if self.commits is None:
            return None
        return self.commits.get_for_message(message_id)

    async def ask(
        self,
        session_id: str,
        request_text: str,
        *,
        task_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SessionAskResult:
        """持久化 user、执行当前分支 Task、保存 assistant 并尝试记录版本。"""

        if self.agent is None:
            raise RuntimeError("Session 问答失败：未配置 AgentLoop")
        if not request_text.strip():
            raise ValueError("Session 问答失败：request_text 不能为空")
        session = self.repository.get(session_id)
        if session is None:
            raise ValueError(f"Session 问答失败：Session 不存在：{session_id}")
        effective_task_id = task_id or uuid.uuid4().hex
        if self.context_builder is not None:
            context = self.context_builder.build(
                session_id,
                request_text,
                task_context={"task_id": effective_task_id, "status": "pending"},
            ).messages
        else:
            history = self.repository.current_path(session_id)
            context = tuple(
                ChatMessage(role=item["role"], content=item["content"]) for item in history
            )
        self._reject_duplicate_task_if_needed(
            session_id, request_text, session["active_leaf_id"], effective_task_id
        )
        baseline = self._read_baseline()
        retry_of_message_id = self._retry_source(session["active_leaf_id"])
        user_message = self.repository.add_message(
            session_id,
            "user",
            request_text,
            parent_message_id=session["active_leaf_id"],
            execution_status="pending",
            retry_of_message_id=retry_of_message_id,
        )
        try:
            result = await self.agent.run(
                request_text,
                task_id=effective_task_id,
                cancel_event=cancel_event,
                context_messages=context,
            )
        except asyncio.CancelledError:
            self._ensure_cancelled_task(effective_task_id, request_text)
            self.repository.update_message_execution(
                user_message["message_id"], effective_task_id, TaskStatus.CANCELLED.value
            )
            raise
        except TaskAlreadyExistsError:
            self.repository.update_message_execution(
                user_message["message_id"], None, "rejected"
            )
            raise
        except Exception as exc:
            message = redact_text(f"Session Task 执行失败：{type(exc).__name__}: {exc}")
            self._ensure_failed_task(effective_task_id, request_text, type(exc).__name__, message)
            self.repository.update_message_execution(
                user_message["message_id"], effective_task_id, TaskStatus.FAILED.value
            )
            raise
        self._ensure_result_task(result, request_text)
        self.repository.update_message_execution(
            user_message["message_id"], result.task_id, result.status.value
        )
        title = session["title"]
        assistant_message_id: str | None = None
        commit_sha: str | None = None
        commit_reason: str | None = None
        if result.status is TaskStatus.SUCCESS:
            assistant = self.repository.add_message(
                session_id,
                "assistant",
                result.content,
                parent_message_id=user_message["message_id"],
                task_id=result.task_id,
            )
            assistant_message_id = assistant["message_id"]
            commit_sha, commit_reason = self._try_record_commit(result.task_id, baseline)
            self._try_persist_version_reason(assistant_message_id, commit_reason)
            if self.repository.assistant_count(session_id) == 1 and title == DEFAULT_SESSION_TITLE:
                generated_title = await self._try_generate_title(request_text, result.content)
                if generated_title:
                    self.repository.set_title(session_id, generated_title)
                    title = generated_title
        return SessionAskResult(
            session_id=session_id,
            task_id=result.task_id,
            result=result,
            user_message_id=user_message["message_id"],
            assistant_message_id=assistant_message_id,
            commit_sha=commit_sha,
            title=title,
            commit_reason=commit_reason,
        )

    def _try_record_commit(
        self, task_id: str, baseline: Any
    ) -> tuple[str | None, str | None]:
        """仅在任务有成功代码写入且前后 Commit 可证明变化时建立关联。"""

        try:
            if self.commits is None or self.git_reader is None:
                return None, "未记录版本：运行时未配置 Git 版本关联能力"
            has_mutation, mutation_reason = self._code_mutation_eligibility(task_id)
            if not has_mutation:
                return None, mutation_reason or "未记录版本：任务未成功执行可版本化的代码写入或修改"
            if baseline.commit_sha is None:
                return None, self._snapshot_reason(
                    baseline, "任务开始前无法确认干净的 Git 基线"
                )
            snapshot = self.git_reader.read_clean_commit()
            if snapshot.commit_sha is None:
                return None, self._snapshot_reason(
                    snapshot, "任务结束时无法确认干净的 Git 工作区"
                )
            if snapshot.commit_sha == baseline.commit_sha:
                return None, "未记录版本：任务前后 HEAD 未变化，无法证明该 Commit 代表本次结果"
            association = self.commits.record(
                task_id,
                snapshot.commit_sha,
                str(self.git_reader.repository_root),
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"未记录版本：版本附加链路失败：{type(exc).__name__}"
        return association["commit_sha"], None

    def _read_baseline(self):
        """在任务开始前读取一次 Git 快照，失败原因保留为安全诊断文本。"""

        if self.git_reader is None:
            return GitCommitSnapshot(None, "Git 版本读取器未配置")
        try:
            return self.git_reader.read_clean_commit()
        except Exception as exc:  # noqa: BLE001
            return GitCommitSnapshot(
                None, f"Git 基线读取失败：{type(exc).__name__}"
            )

    def _code_mutation_eligibility(self, task_id: str) -> tuple[bool, str | None]:
        """查询成功代码修改资格，并把审计故障转换成版本旁路诊断。"""

        try:
            audit_repository = self.agent.executor.audit.repository  # type: ignore[union-attr]
            if audit_repository.has_successful_code_mutation(task_id):
                return True, None
            return False, "未记录版本：任务未成功执行可版本化的代码写入或修改"
        except Exception as exc:  # noqa: BLE001
            return False, f"未记录版本：代码修改资格查询失败：{type(exc).__name__}"

    def _reject_duplicate_task_if_needed(
        self,
        session_id: str,
        request_text: str,
        parent_message_id: str | None,
        task_id: str,
    ) -> None:
        """在可见消息写入前拒绝重复 Task ID，避免新消息借用旧执行事实。"""

        try:
            existing = self.agent.task_store.get(task_id)  # type: ignore[union-attr]
        except Exception as exc:
            raise SessionError(
                f"Session Task 去重检查失败：无法读取 Task {task_id}，原因：{type(exc).__name__}"
            ) from exc
        if existing is None:
            return
        self.repository.add_message(
            session_id,
            "user",
            request_text,
            parent_message_id=parent_message_id,
            execution_status="rejected",
        )
        raise TaskAlreadyExistsError(
            f"任务创建失败：task_id 已存在：{task_id}，本次请求未创建新 Task"
        )

    def _try_persist_version_reason(
        self, assistant_message_id: str, reason: str | None
    ) -> None:
        """版本原因落库失败只降级展示信息，不影响已保存的 assistant。"""

        try:
            self.repository.set_message_version(assistant_message_id, reason)
        except Exception:  # noqa: BLE001
            return

    def _retry_source(self, message_id: str | None) -> str | None:
        """失败或取消的 user 叶子再次提问时记录稳定的重试来源。"""

        if message_id is None:
            return None
        message = self.repository.get_message(message_id)
        if message and message.get("role") == "user" and message.get("execution_status") in {
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }:
            return message_id
        return None

    def _ensure_result_task(self, result: AgentResult, request_text: str) -> None:
        """为完全访问确认拒绝等未建 Task 的 AgentResult 补齐可审计任务边界。"""

        if self.agent is None:
            return
        try:
            created = self.agent.task_store.create(
                result.task_id, request_text, self.agent.review_mode
            )
            if created and result.status is TaskStatus.SUCCESS:
                self.agent.task_store.set_status(result.task_id, TaskStatus.RUNNING)
                self.agent.task_store.set_status(
                    result.task_id, TaskStatus.SUCCESS, result_summary=result.content
                )
            elif created and result.status is not TaskStatus.SUCCESS:
                self.agent.task_store.set_status(
                    result.task_id,
                    result.status,
                    error_type="SessionResult",
                    error_message=result.error_message,
                )
        except Exception as exc:
            get_task = getattr(self.agent.task_store, "get", None)
            if not callable(get_task) or get_task(result.task_id) is None:
                raise SessionError(
                    f"Session Task 关联失败：无法建立 Task {result.task_id}，原因：{type(exc).__name__}"
                ) from exc

    def _ensure_cancelled_task(self, task_id: str, request_text: str) -> None:
        """协程在 AgentLoop 边界外取消时补建 cancelled Task。"""

        if self.agent is None:
            return
        created = self.agent.task_store.create(task_id, request_text, self.agent.review_mode)
        if created:
            self.agent.task_store.set_status(
                task_id,
                TaskStatus.CANCELLED,
                error_type="CancelledError",
                error_message="Session 问答协程已取消",
            )

    def _ensure_failed_task(
        self, task_id: str, request_text: str, error_type: str, error_message: str
    ) -> None:
        """Session 捕获执行异常时建立 failed Task，保留可定位但已脱敏的原因。"""

        if self.agent is None:
            return
        created = self.agent.task_store.create(task_id, request_text, self.agent.review_mode)
        if created:
            self.agent.task_store.set_status(
                task_id,
                TaskStatus.FAILED,
                error_type=error_type,
                error_message=error_message,
            )

    @staticmethod
    def _snapshot_reason(snapshot: Any, fallback: str) -> str:
        reason = getattr(snapshot, "reason", None)
        return f"未记录版本：{reason or fallback}"

    async def _try_generate_title(self, request_text: str, answer: str) -> str | None:
        """标题调用失败、取消或空响应时保持默认标题。"""

        if self.backend is None:
            return None
        messages = (
            ChatMessage(role="system", content=_TITLE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=redact_text(request_text)),
            ChatMessage(role="assistant", content=redact_text(answer)),
        )
        try:
            turn = await self.backend.complete(messages, ())
        except asyncio.CancelledError:
            return None
        except Exception:  # noqa: BLE001
            return None
        title = redact_text(turn.content).strip().splitlines()[0] if turn.content.strip() else ""
        title, _ = truncate_text(title, 80)
        return title or None
