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
    """使用小型 JSON 文件保存本机默认审查模式和活动 Session。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load_review_mode(self) -> StoredReviewMode:
        """读取偏好；损坏、未知值或读取失败时安全降级为 strict。"""

        try:
            payload = self._read_payload()
            if not payload:
                return StoredReviewMode(None)
            mode = parse_review_mode(payload["default_review_mode"])
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return StoredReviewMode(
                None,
                "本地审查模式偏好读取失败，已安全降级为 strict，"
                f"原因：{type(exc).__name__}",
            )
        return StoredReviewMode(mode)

    def load_active_session_id(self) -> str | None:
        """读取当前 CLI 默认 Session，损坏或非法值按未选择处理。"""

        try:
            value = self._read_payload().get("active_session_id")
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if isinstance(value, str) and 0 < len(value) <= 128 and value.replace("-", "").isalnum():
            return value
        return None

    def save_review_mode(self, mode: ReviewMode | str) -> None:
        """原子保存模式，避免进程中断留下半个 JSON 文件。"""

        try:
            parsed = parse_review_mode(mode)
            try:
                payload = self._read_payload()
            except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                payload = {}
            payload.update({"version": 1, "default_review_mode": parsed.value})
            self._write_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            raise PreferenceError(
                f"本地审查模式偏好保存失败：目标 {self.path}，原因：{type(exc).__name__}"
            ) from exc

    def save_active_session_id(self, session_id: str) -> None:
        """原子保存 CLI 默认 Session，不覆盖既有审查模式偏好。"""

        if not isinstance(session_id, str) or not 0 < len(session_id) <= 128:
            raise PreferenceError("活动 Session 保存失败：Session 标识格式无效")
        if not session_id.replace("-", "").isalnum():
            raise PreferenceError("活动 Session 保存失败：Session 标识包含不允许字符")
        try:
            try:
                payload = self._read_payload()
            except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                payload = {}
            payload.update({"version": 1, "active_session_id": session_id})
            self._write_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            raise PreferenceError(
                f"活动 Session 保存失败：目标 {self.path}，原因：{type(exc).__name__}"
            ) from exc

    def clear_active_session(self) -> None:
        """清除已删除 Session 的本机默认选择。"""

        try:
            payload = self._read_payload()
            if "active_session_id" in payload:
                payload.pop("active_session_id")
                self._write_payload(payload)
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise PreferenceError(
                f"活动 Session 清理失败：目标 {self.path}，原因：{type(exc).__name__}"
            ) from exc

    def _read_payload(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise OSError("偏好文件是符号链接")
        if self.path.stat().st_size > 8 * 1024:
            raise ValueError("偏好文件超过允许大小")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("偏好文件根节点不是对象")
        return payload

    def _write_payload(self, payload: dict[str, object]) -> None:
        if self.path.is_symlink():
            raise OSError("偏好文件是符号链接")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        temporary_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".preferences-", suffix=".tmp", dir=self.path.parent
            )
            temporary_path = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
