"""脱敏工具：被审计、Bash 输出和模型错误处理共同调用，阻止密钥等敏感信息外泄。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|cookie|password|passwd|secret|authorization)", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<key>api[_-]?key|token|cookie|password|passwd|secret|authorization)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def is_sensitive_key(key: str) -> bool:
    """判断字段名是否可能包含认证或凭据。"""

    return bool(_SENSITIVE_KEY.search(key))


def redact_value(value: Any) -> Any:
    """递归脱敏字典、列表和元组，保留非敏感数据结构。"""

    if isinstance(value, dict):
        return {
            key: "[已脱敏]" if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def redact_arguments(value: Any) -> str:
    """把工具参数转换为稳定 JSON，并在无法序列化时给出安全摘要。"""

    try:
        safe_value = redact_value(value)
        return redact_text(json.dumps(safe_value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return "[参数已脱敏：无法序列化]"


def content_sha256(value: str | bytes) -> str:
    """返回内容摘要，不把正文写入审计或审批记录。"""

    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def action_fingerprint(value: Any) -> str:
    """为审批动作生成稳定指纹，用于检测审批后参数或目标状态变化。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def audit_text_summary(label: str, value: str) -> str:
    """仅保存文本长度和摘要哈希，避免正文进入 SQLite。"""

    return f"{label}：字节数={len(value.encode('utf-8'))}，sha256={content_sha256(value)}"


def redact_text(text: str) -> str:
    """脱敏常见 key=value、key:value 和 Bearer 令牌。"""

    text = _SENSITIVE_ASSIGNMENT.sub(r"\g<key>\g<sep>[已脱敏]", text)
    return _BEARER.sub(r"\1[已脱敏]", text)


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节上限截断文本，避免多字节字符被切坏。"""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
