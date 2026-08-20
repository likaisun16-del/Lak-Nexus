"""Embedding Provider 测试：验证豆包/火山方舟兼容接口的配置、请求和响应校验。"""

from __future__ import annotations

import json
import urllib.error
from typing import Self

import pytest

from likai_nexus.config import Settings
from likai_nexus.errors import ConfigError, ModelBackendError
from likai_nexus.models.embedding import DoubaoEmbeddingProvider, create_embedding_provider


class FakeResponse:
    """为标准库 HTTP 调用提供最小响应替身。"""

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_doubao_provider_calls_configured_embeddings_endpoint(monkeypatch) -> None:
    provider = DoubaoEmbeddingProvider(
        base_url="https://ark.example.test/api/v3",
        api_key="test-key",
        model="doubao-test",
        dimension=2,
        timeout_seconds=7,
    )
    calls: list[tuple[str, int]] = []

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        calls.append((request.full_url, timeout))
        assert request.get_header("Authorization") == "Bearer test-key"
        assert json.loads(request.data.decode("utf-8")) == {
            "model": "doubao-test",
            "input": ["用户偏好"],
            "encoding_format": "float",
        }
        return FakeResponse({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert provider.embed("用户偏好") == (0.1, 0.2)
    assert calls == [("https://ark.example.test/api/v3/embeddings", 7)]


def test_embedding_provider_factory_requires_key_only_when_enabled(tmp_path) -> None:
    settings = Settings(workspace_root=tmp_path, embedding_provider="doubao")

    with pytest.raises(ConfigError, match="EMBEDDING_API_KEY"):
        create_embedding_provider(settings)


def test_embedding_provider_factory_keeps_default_disabled(tmp_path) -> None:
    settings = Settings(workspace_root=tmp_path)

    assert create_embedding_provider(settings) is None


def test_embedding_provider_rejects_wrong_dimension_without_exposing_key(monkeypatch) -> None:
    secret = "embedding-secret"
    provider = DoubaoEmbeddingProvider(
        base_url="https://ark.example.test/api/v3",
        api_key=secret,
        model="doubao-test",
        dimension=2,
    )

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: FakeResponse({"data": [{"embedding": [0.1]}]}),
    )

    with pytest.raises(ModelBackendError, match="向量维度") as exc_info:
        provider.embed("测试")

    assert secret not in str(exc_info.value)


def test_embedding_provider_redacts_http_error_body(monkeypatch) -> None:
    secret = "embedding-secret"

    def fake_urlopen(request, timeout: int):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            f"invalid key {secret}",
            hdrs=None,
            fp=None,
        )

    provider = DoubaoEmbeddingProvider(
        base_url="https://ark.example.test/api/v3",
        api_key=secret,
        model="doubao-test",
        dimension=2,
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ModelBackendError, match="HTTP 状态码 401") as exc_info:
        provider.embed("测试")

    assert secret not in str(exc_info.value)
