"""Git 只读版本读取：检查当前工程是否干净并返回完整 HEAD SHA，不执行写操作。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_FULL_COMMIT_SHA = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


@dataclass(frozen=True, slots=True)
class GitCommitSnapshot:
    """一次只读 Git 检查结果；commit_sha 为空表示不可安全关联。"""

    commit_sha: str | None
    reason: str | None = None


class GitReadOnly:
    """只调用 rev-parse 和 status，拒绝把未提交工作树描述为结果版本。"""

    def __init__(self, repository_root: Path, timeout_seconds: float = 5.0) -> None:
        self.repository_root = Path(repository_root).resolve(strict=False)
        self.timeout_seconds = timeout_seconds

    def read_clean_commit(self) -> GitCommitSnapshot:
        """读取 HEAD 和工作树状态；任何异常都降级为不可关联结果。"""

        head = self._run_readonly(("rev-parse", "--verify", "HEAD^{commit}"))
        if head is None:
            return GitCommitSnapshot(None, "Git Commit 读取失败：无法读取当前 HEAD")
        status = self._run_readonly(("status", "--porcelain=v1", "--untracked-files=all"))
        if status is None:
            return GitCommitSnapshot(None, "Git 工作区状态读取失败：无法确认未提交变更")
        if status.strip():
            return GitCommitSnapshot(None, "Git 工作区存在未提交变更，未记录版本")
        commit_sha = head.strip().lower()
        if not _FULL_COMMIT_SHA.fullmatch(commit_sha):
            return GitCommitSnapshot(None, "Git Commit 读取失败：HEAD 不是完整 Commit SHA")
        return GitCommitSnapshot(commit_sha)

    def _run_readonly(self, arguments: tuple[str, ...]) -> str | None:
        """以参数数组执行单个只读 Git 查询，不启用 shell。"""

        try:
            environment = os.environ.copy()
            # status 默认可能刷新 index 的 stat 缓存；该检查必须对仓库完全无副作用。
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            result = subprocess.run(
                ["git", "--no-optional-locks", *arguments],
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout
