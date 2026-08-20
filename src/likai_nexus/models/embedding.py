"""Embedding 模型适配：提供豆包/火山方舟文本向量 API 入口，供记忆向量索引调用。"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from ..config import Settings
from ..errors import ConfigError, ModelBackendError
from ..memory.contracts import EmbeddingProvider


class OpenAICompatibleEmbeddingProvider:
    """调用 OpenAI 兼容的 Embeddings HTTP 接口，避免把供应商 SDK 带入记忆层。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout_seconds: int = 60,
    ) -> None:
        if not base_url.strip():
            raise ConfigError("Embedding 配置错误：EMBEDDING_BASE_URL 不能为空")
        if not api_key.strip():
            raise ConfigError("Embedding 配置错误：缺少 EMBEDDING_API_KEY")
        if not model.strip():
            raise ConfigError("Embedding 配置错误：EMBEDDING_MODEL 不能为空")
        if dimension <= 0:
            raise ConfigError("Embedding 配置错误：EMBEDDING_DIMENSION 必须大于 0")
        if timeout_seconds <= 0:
            raise ConfigError("Embedding 配置错误：EMBEDDING_TIMEOUT_SECONDS 必须大于 0")
        normalized_url = base_url.rstrip("/")
        if not normalized_url.startswith(("http://", "https://")):
            raise ConfigError(
                "Embedding 配置错误：EMBEDDING_BASE_URL 必须使用 http:// 或 https://"
            )
        self._url = (
            normalized_url
            if normalized_url.lower().endswith("/embeddings")
            else f"{normalized_url}/embeddings"
        )
        self._api_key = api_key
        self._model = model
        self._dimension = dimension
        self._timeout_seconds = timeout_seconds

    @property
    def dimension(self) -> int:
        """返回当前模型必须输出的向量维度。"""

        return self._dimension

    def embed(self, text: str) -> Sequence[float]:
        """把一条文本转换为向量，并校验维度及数值合法性。"""

        if not isinstance(text, str) or not text.strip():
            raise ModelBackendError("Embedding 请求失败：输入文本不能为空")
        request = urllib.request.Request(
            self._url,
            data=json.dumps(
                {"model": self._model, "input": [text], "encoding_format": "float"},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ModelBackendError(
                f"Embedding 请求失败：HTTP 状态码 {exc.code}，请检查模型地址、接入点和权限"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelBackendError(
                f"Embedding 请求失败：无法连接模型服务，原因：{type(exc).__name__}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelBackendError(
                f"Embedding 响应解析失败：响应不是有效 JSON，原因：{type(exc).__name__}"
            ) from exc
        return self._parse_embedding(payload)

    def _parse_embedding(self, payload: Any) -> tuple[float, ...]:
        try:
            values = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelBackendError(
                "Embedding 响应解析失败：缺少 data[0].embedding"
            ) from exc
        if not isinstance(values, (list, tuple)):
            raise ModelBackendError("Embedding 响应解析失败：embedding 必须是数字数组")
        if len(values) != self._dimension:
            raise ModelBackendError(
                f"Embedding 响应校验失败：向量维度为 {len(values)}，期望 {self._dimension}"
            )
        result: list[float] = []
        for index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelBackendError(
                    f"Embedding 响应校验失败：第 {index} 个向量值不是数字"
                )
            converted = float(value)
            if not math.isfinite(converted):
                raise ModelBackendError(
                    f"Embedding 响应校验失败：第 {index} 个向量值不是有限数"
                )
            result.append(converted)
        return tuple(result)


class DoubaoEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    """豆包文本向量 Provider；通过配置即可切换火山方舟模型或接入点。"""


def create_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """按配置创建 Provider；未启用时返回 None，让 SQLite 召回继续可用。"""

    provider = settings.embedding_provider.strip().lower()
    if provider in {"", "none", "disabled"}:
        return None
    if provider != "doubao":
        raise ConfigError(
            f"Embedding 配置错误：不支持的 EMBEDDING_PROVIDER={settings.embedding_provider!r}"
        )
    if not settings.embedding_api_key:
        raise ConfigError("Embedding 配置错误：启用 doubao 时缺少 EMBEDDING_API_KEY")
    return DoubaoEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
