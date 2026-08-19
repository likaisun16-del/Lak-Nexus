"""本地偏好存储：原子保存默认审查模式，不保存任务正文或敏感凭据。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import PreferenceError
from ..safety.review_mode import ReviewMode, parse_review_mode


@dataclass(frozen=True, slots=True)
class StoredReviewMode:
    """本地偏好读取结果；mode 为 None 表示没有可用偏好。"""

    mode: ReviewMode | None
    warning: str | None = None


class LocalPreferenceStore:
    """使用小型 JSON 文件保存本机默认审查模式。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load_review_mode(self) -> StoredReviewMode:
        """读取偏好；损坏、未知值或读取失败时安全降级为 strict。"""

        if not self.path.exists():
            return StoredReviewMode(None)
        try:
            if self.path.is_symlink():
                raise OSError("偏好文件是符号链接")
            if self.path.stat().st_size > 8 * 1024:
                raise ValueError("偏好文件超过允许大小")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            mode = parse_review_mode(payload["default_review_mode"])
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return StoredReviewMode(
                None,
                "本地审查模式偏好读取失败，已安全降级为 strict，"
                f"原因：{type(exc).__name__}",
            )
        return StoredReviewMode(mode)

    def save_review_mode(self, mode: ReviewMode | str) -> None:
        """原子保存模式，避免进程中断留下半个 JSON 文件。"""

        try:
            parsed = parse_review_mode(mode)
            if self.path.is_symlink():
                raise OSError("偏好文件是符号链接")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": 1, "default_review_mode": parsed.value},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            temporary_path: Path | None = None
            try:
                descriptor, name = tempfile.mkstemp(
                    prefix=".preferences-", suffix=".tmp", dir=self.path.parent
                )
                temporary_path = Path(name)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
            except OSError:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                raise
        except (OSError, ValueError, TypeError) as exc:
            raise PreferenceError(
                f"本地审查模式偏好保存失败：目标 {self.path}，原因：{type(exc).__name__}"
            ) from exc
