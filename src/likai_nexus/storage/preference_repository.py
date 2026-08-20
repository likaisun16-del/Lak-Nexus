"""数据库用户偏好仓储：保存可覆盖的单用户键值并供上下文组装使用。"""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import PreferenceError
from ..safety.redaction import is_sensitive_key, redact_text
from .database import Database
from .postgres import PostgresDatabase
from .task_repository import utc_now

_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ALLOWED_SOURCES = frozenset({"user", "system"})
_MAX_VALUE_BYTES = 8 * 1024


class PreferenceRepository:
    """通过当前数据库读写安全的 JSON 偏好值。"""

    def __init__(self, database: Database | PostgresDatabase) -> None:
        self.database = database

    def get(self, key: str, default: Any = None) -> Any:
        """读取偏好；缺失、损坏或不安全值统一返回调用方默认值。"""

        self._validate_key(key)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM preferences WHERE preference_key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            value = json.loads(row[0])
            self._validate_value(value)
        except (PreferenceError, TypeError, ValueError, json.JSONDecodeError):
            return default
        return value

    def get_record(self, key: str) -> dict[str, Any] | None:
        """读取包含来源和更新时间的偏好记录；损坏记录按不可用处理。"""

        self._validate_key(key)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT preference_key, value_json, source, updated_at "
                "FROM preferences WHERE preference_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[1])
            self._validate_value(value)
        except (PreferenceError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return {
            "preference_key": row[0],
            "value": value,
            "source": row[2],
            "updated_at": row[3],
        }

    def set(self, key: str, value: Any, source: str = "user") -> dict[str, Any]:
        """覆盖偏好；系统来源不能静默覆盖用户已明确设置的值。"""

        self._validate_key(key)
        self._validate_source(source)
        self._validate_value(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise PreferenceError(
                f"偏好保存失败：{key} 的 JSON 值超过 {_MAX_VALUE_BYTES} 字节限制"
            )
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT source FROM preferences WHERE preference_key = ?", (key,)
            ).fetchone()
            if existing is not None and existing[0] == "user" and source == "system":
                raise PreferenceError(f"偏好保存失败：系统来源不能覆盖用户偏好：{key}")
            connection.execute(
                "INSERT INTO preferences(preference_key, value_json, source, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(preference_key) DO UPDATE SET value_json=excluded.value_json, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (key, encoded, source, utc_now()),
            )
        return self.get_record(key)  # type: ignore[return-value]

    def delete(self, key: str) -> bool:
        """删除一个偏好值，不影响其他偏好。"""

        self._validate_key(key)
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM preferences WHERE preference_key = ?", (key,)
            )
            return cursor.rowcount == 1

    def list(self) -> list[dict[str, Any]]:
        """按键稳定返回所有可解析偏好，不把损坏原文暴露给调用方。"""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT preference_key FROM preferences ORDER BY preference_key"
            ).fetchall()
        records = [self.get_record(row[0]) for row in rows]
        return [record for record in records if record is not None]

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
            raise PreferenceError(f"偏好校验失败：键名格式无效：{key!r}")
        if is_sensitive_key(key):
            raise PreferenceError(f"偏好校验失败：键名可能包含敏感凭据：{key}")

    @staticmethod
    def _validate_source(source: str) -> None:
        if not isinstance(source, str) or source not in _ALLOWED_SOURCES:
            allowed = ", ".join(sorted(_ALLOWED_SOURCES))
            raise PreferenceError(f"偏好校验失败：来源 {source!r} 不允许，允许值为：{allowed}")

    @staticmethod
    def _validate_value(value: Any) -> None:
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise PreferenceError(
                f"偏好校验失败：值无法编码为 JSON，原因：{type(exc).__name__}"
            ) from exc
        PreferenceRepository._reject_sensitive_value(value)

    @staticmethod
    def _reject_sensitive_value(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if is_sensitive_key(str(key)):
                    raise PreferenceError(f"偏好校验失败：值包含敏感字段：{key}")
                PreferenceRepository._reject_sensitive_value(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                PreferenceRepository._reject_sensitive_value(item)
            return
        if isinstance(value, str) and redact_text(value) != value:
            raise PreferenceError("偏好校验失败：值可能包含 Token、密码或私钥内容")
