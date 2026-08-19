"""脱敏工具：被审计、Bash 输出和模型错误处理共同调用，阻止密钥等敏感信息外泄。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|private[_-]?key|token|cookie|password|passwd|"
    r"secret|credential|authorization|signing[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|"
    r"cookie|password|passwd|secret|credential|authorization|signing[_-]?key)"
    r"[A-Za-z0-9_-]*)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_SENSITIVE_JSON = re.compile(
    r'(?P<key>"?[A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|'
    r'cookie|password|passwd|secret|credential|authorization|signing[_-]?key)'
    r'[A-Za-z0-9_-]*"?\s*:\s*)'
    r'(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
    re.IGNORECASE,
)
_ANSI_ESCAPE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])"
)
_TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNLABELED_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_SAFE_AUDIT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


def is_sensitive_key(key: str) -> bool:
    """判断字段名是否可能包含认证或凭据。"""

    return bool(_SENSITIVE_KEY.search(key))


def safe_audit_identifier(value: object, label: str, *, trusted: bool = False) -> str:
    """不可信标识符默认只保存固定标签和摘要，内部规范值可保留可读性。"""

    text = str(value)
    if trusted and _SAFE_AUDIT_IDENTIFIER.fullmatch(text) and not is_sensitive_key(text):
        return text
    return f"{label}:{content_sha256(text)[:16]}"


def redact_value(value: Any) -> Any:
    """递归脱敏字典、列表和元组，保留非敏感数据结构。"""

    if isinstance(value, dict):
        return {
            redact_text(str(key)): "[已脱敏]" if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
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


def sanitize_terminal_text(text: str) -> str:
    """移除终端控制序列，保留换行和制表，避免输出覆盖或伪造终端内容。"""

    text = _ANSI_ESCAPE.sub("", text)
    text = text.replace("\r", "\n")
    return _TERMINAL_CONTROL.sub("", text)


def sanitize_terminal_value(
    value: Any, *, _depth: int = 0, _seen: frozenset[int] | None = None
) -> Any:
    """递归清理界面事件字符串，并对循环或过深结构返回安全占位符。"""

    if _depth > 20:
        return "[展示结构已截断]"
    if isinstance(value, (dict, list, tuple)):
        seen = _seen or frozenset()
        identity = id(value)
        if identity in seen:
            return "[展示结构已循环]"
        seen = seen | {identity}
        if isinstance(value, dict):
            return {
                sanitize_terminal_text(str(key)): sanitize_terminal_value(
                    item, _depth=_depth + 1, _seen=seen
                )
                for key, item in value.items()
            }
        return [
            sanitize_terminal_value(item, _depth=_depth + 1, _seen=seen)
            for item in value
        ]
    if isinstance(value, str):
        return sanitize_terminal_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[展示值不可用]"


def redact_text(text: str) -> str:
    """脱敏常见凭据赋值、JSON 令牌、Bearer 令牌和 PEM 私钥块。"""

    text = _PRIVATE_KEY_BLOCK.sub("[已脱敏：私钥内容]", text)
    text = _UNLABELED_CREDENTIAL.sub("[已脱敏：凭据]", text)
    text = _SENSITIVE_ASSIGNMENT.sub(r"\g<key>\g<sep>[已脱敏]", text)
    text = _SENSITIVE_JSON.sub(r"\g<key>\g<quote>[已脱敏]\g<quote>", text)
    return _BEARER.sub(r"\1[已脱敏]", text)


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节上限截断文本，避免多字节字符被切坏。"""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
