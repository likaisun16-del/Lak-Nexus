"""Session 领域服务：组织当前分支问答、标题生成和 Task 的只读 Git 关联。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..git import GitReadOnly
from ..models.base import ModelBackend
from ..orchestrator.agent_loop import AgentLoop
from ..orchestrator.schemas import AgentResult, ChatMessage, TaskStatus
from ..safety.redaction import redact_text, truncate_text
from ..storage.commit_repository import CommitRepository
from ..storage.session_repository import DEFAULT_SESSION_TITLE, SessionRepository

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
    ) -> None:
        self.repository = repository
        self.agent = agent
        self.backend = backend
        self.commits = commits
        self.git_reader = git_reader

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

    def continue_from(self, message_id: str) -> str:
        """把活动叶子切换到任意合法可见消息并返回其 Session。"""

        session_id = self.repository.get_session_for_message(message_id)
        if session_id is None:
            raise ValueError(f"继续会话失败：消息不存在：{message_id}")
        self.repository.set_active_leaf(session_id, message_id)
        return session_id

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
        history = self.repository.current_path(session_id)
        context = tuple(
            ChatMessage(role=item["role"], content=item["content"]) for item in history
        )
        user_message = self.repository.add_message(
            session_id,
            "user",
            request_text,
            parent_message_id=session["active_leaf_id"],
        )
        result = await self.agent.run(
            request_text,
            task_id=task_id,
            cancel_event=cancel_event,
            context_messages=context,
        )
        title = session["title"]
        assistant_message_id: str | None = None
        commit_sha: str | None = None
        if result.status is TaskStatus.SUCCESS:
            assistant = self.repository.add_message(
                session_id,
                "assistant",
                result.content,
                parent_message_id=user_message["message_id"],
                task_id=result.task_id,
            )
            assistant_message_id = assistant["message_id"]
            commit_sha = self._try_record_commit(result.task_id)
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
        )

    def _try_record_commit(self, task_id: str) -> str | None:
        """Git 或关联库失败时只返回未记录，不改变已成功 Task。"""

        if self.commits is None or self.git_reader is None:
            return None
        snapshot = self.git_reader.read_clean_commit()
        if snapshot.commit_sha is None:
            return None
        try:
            association = self.commits.record(
                task_id,
                snapshot.commit_sha,
                str(self.git_reader.repository_root),
            )
        except Exception:  # noqa: BLE001
            return None
        return association["commit_sha"]

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
