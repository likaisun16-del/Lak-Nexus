"""OpenAI 兼容模型后端：只负责 HTTP 协议转换，不把供应商对象带入编排层。"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from typing import Any

from ..config import Settings
from ..errors import ModelBackendError
from ..orchestrator.schemas import AssistantTurn, ChatMessage, ToolCall, ToolSpec


class OpenAICompatibleBackend:
    """通过标准库调用 OpenAI Chat Completions 兼容接口。"""

    def __init__(self, settings: Settings) -> None:
        if not settings.api_key:
            raise ModelBackendError("模型配置失败：缺少 OPENAI_API_KEY，无法调用真实模型")
        self.settings = settings

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        cancel_event: asyncio.Event | None = None,
    ) -> AssistantTurn:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        payload = {
            "model": self.settings.model,
            "messages": [self._message_to_payload(message) for message in messages],
            "tools": [self._tool_to_payload(tool) for tool in tools],
            "tool_choice": "auto",
        }
        response = await asyncio.to_thread(self._request, payload)
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        return self._parse_response(response)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.api_base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.model_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ModelBackendError(
                f"模型请求失败：HTTP 状态码 {exc.code}，请检查模型地址、权限和请求参数"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelBackendError(
                f"模型请求失败：无法连接模型服务，原因：{type(exc).__name__}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelBackendError(f"模型响应解析失败：响应不是有效 JSON，原因：{type(exc).__name__}") from exc

    @staticmethod
    def _message_to_payload(message: ChatMessage) -> dict[str, Any]:
        if message.role == "assistant" and message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.name:
            payload["name"] = message.name
        return payload

    @staticmethod
    def _tool_to_payload(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _parse_response(response: dict[str, Any]) -> AssistantTurn:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelBackendError("模型响应解析失败：缺少 choices[0].message") from exc
        content = message.get("content") or ""
        calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            try:
                function = raw_call["function"]
                name = function["name"]
                raw_arguments = function.get("arguments", "{}")
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("工具参数不是 JSON 对象")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ModelBackendError(
                    f"模型响应解析失败：工具调用参数无效，原因：{type(exc).__name__}"
                ) from exc
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or uuid.uuid4().hex),
                    name=str(name),
                    arguments=arguments,
                )
            )
        return AssistantTurn(content=str(content), tool_calls=tuple(calls))
