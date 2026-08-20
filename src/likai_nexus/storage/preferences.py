"""数据库偏好适配：CLI 偏好保存到当前数据库，旧 JSON 仅用于一次性迁移。"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import PreferenceError
from ..safety.review_mode import ReviewMode, parse_review_mode
from .preference_repository import PreferenceRepository

_LEGACY_KEYS = frozenset({"version", "default_review_mode", "active_session_id"})
_MAX_LEGACY_FILE_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class StoredReviewMode:
    """数据库偏好读取结果；mode 为 None 表示没有可用偏好。"""

    mode: ReviewMode | None
    warning: str | None = None


class DatabasePreferenceStore:
    """使用当前数据库保存 CLI 默认审查模式和活动 Session。"""

    def __init__(self, repository: PreferenceRepository) -> None:
        self.repository = repository

    def load_review_mode(self) -> StoredReviewMode:
        """读取数据库偏好；缺失或非法值安全降级为 strict。"""

        record = self.repository.get_record("default_review_mode")
        if record is None:
            return StoredReviewMode(None)
        try:
            mode = parse_review_mode(record["value"])
        except (KeyError, TypeError, ValueError) as exc:
            return StoredReviewMode(
                None,
                "数据库审查模式偏好不可用，已安全降级为 strict，"
                f"原因：{type(exc).__name__}",
            )
        return StoredReviewMode(mode)

    def load_active_session_id(self) -> str | None:
        """读取数据库中的活动 Session，缺失或非法值按未选择处理。"""

        record = self.repository.get_record("active_session_id")
        if record is None:
            return None
        value = record.get("value")
        if self._is_valid_session_id(value):
            return value
        return None

    def save_review_mode(self, mode: ReviewMode | str) -> None:
        """保存 CLI 默认审查模式。"""

        self.repository.set("default_review_mode", parse_review_mode(mode).value)

    def save_active_session_id(self, session_id: str) -> None:
        """保存用户当前选择的活动 Session。"""

        if not self._is_valid_session_id(session_id):
            raise PreferenceError("活动 Session 保存失败：Session 标识格式无效")
        self.repository.set("active_session_id", session_id)

    def clear_active_session(self) -> None:
        """清除已删除 Session 的数据库偏好。"""

        self.repository.delete("active_session_id")

    @staticmethod
    def _is_valid_session_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= 128
            and value.replace("-", "").isalnum()
        )


def migrate_legacy_preference_file(
    repository: PreferenceRepository, path: Path
) -> tuple[str, ...]:
    """把旧 preferences.json 导入数据库并改名归档，重复执行保持幂等。"""

    source = Path(path)
    if not source.exists():
        return ()
    try:
        payload = _read_legacy_payload(source)
        values = _extract_legacy_values(payload)
        for key, value in values.items():
            if repository.get_record(key) is None:
                repository.set(key, value)
        archive = _archive_legacy_file(source)
    except (
        OSError,
        shutil.Error,
        UnicodeError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        PreferenceError,
    ) as exc:
        return (
            (
                f"旧偏好迁移跳过：文件 {source} 未写入数据库，已安全降级为 strict，"
                f"原因：{type(exc).__name__}"
            ),
        )
    return (f"旧偏好已迁移到数据库，原文件已归档到 {archive}",)


def _read_legacy_payload(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise OSError("旧偏好文件是符号链接")
    if path.stat().st_size > _MAX_LEGACY_FILE_BYTES:
        raise ValueError("旧偏好文件超过允许大小")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("旧偏好文件根节点不是对象")
    return payload


def _extract_legacy_values(payload: dict[str, Any]) -> dict[str, object]:
    unknown = set(payload) - _LEGACY_KEYS
    if unknown:
        names = ", ".join(sorted(str(item) for item in unknown))
        raise PreferenceError(f"旧偏好迁移失败：发现未知字段：{names}")
    values: dict[str, object] = {}
    if "default_review_mode" in payload:
        values["default_review_mode"] = parse_review_mode(
            payload["default_review_mode"]
        ).value
    if "active_session_id" in payload:
        session_id = payload["active_session_id"]
        if not DatabasePreferenceStore._is_valid_session_id(session_id):
            raise PreferenceError("旧偏好迁移失败：活动 Session 标识格式无效")
        values["active_session_id"] = session_id
    return values


def _archive_legacy_file(path: Path) -> Path:
    archive = path.with_name(f"{path.name}.migrated-{uuid.uuid4().hex[:12]}")
    shutil.move(str(path), str(archive))
    return archive
