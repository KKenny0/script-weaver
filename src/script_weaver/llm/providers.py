"""Provider-agnostic LLM abstraction layer.

Supports: Anthropic, OpenAI, DeepSeek, GLM, Qwen, and any OpenAI-compatible endpoint.
All providers implement the same interface so agents can switch models without code changes.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from script_weaver.core.config import get_settings


# ────────────────────────────────────────────────────────
# Shared Types
# ────────────────────────────────────────────────────────


class Message(BaseModel):
    """A chat message."""
    role: Literal["system", "user", "assistant"]
    content: str


class ToolCall(BaseModel):
    """A tool/function call from the LLM."""
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] | str = Field(default_factory=dict)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """Unified response from any LLM provider."""
    content: str | None = None                       # Text reply (when no tool calls)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    stop_reason: str | None = None                   # "end_turn", "tool_use", etc.


# ────────────────────────────────────────────────────────
# Abstract Base Provider
# ────────────────────────────────────────────────────────


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers."""

    def __init__(self, api_key: str, model: str, **kwargs):
        self.api_key = api_key
        self.model = model
        self._kwargs = kwargs

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        """Send a chat completion request and return a unified response."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name."""


# ────────────────────────────────────────────────────────
# Anthropic Provider
# ────────────────────────────────────────────────────────


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude API provider via httpx."""

    BASE_URL = "https://api.anthropic.com/v1/messages"

    @property
    def provider_name(self) -> str:
        return "Anthropic"

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        # Separate system message for Anthropic API format
        system_content = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg["content"]
            else:
                api_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system_content:
            payload["system"] = system_content
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {
                        "type": "object",
                        "properties": {},
                    }),
                }
                for t in tools
            ]

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self.BASE_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_response(data)

    def _parse_response(self, data: dict) -> ChatResponse:
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
            total_tokens=(
                usage_data.get("input_tokens", 0)
                + usage_data.get("output_tokens", 0)
            ),
        )

        content_text = ""
        tool_calls = []
        stop_reason = data.get("stop_reason")

        for block in data.get("content", []):
            if block.get("type") == "text":
                content_text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))

        return ChatResponse(
            content=content_text or None,
            tool_calls=tool_calls,
            usage=usage,
            model=data.get("model", self.model),
            stop_reason=stop_reason,
        )


# ────────────────────────────────────────────────────────
# OpenAI Provider (base for compatible APIs)
# ────────────────────────────────────────────────────────


class OpenAICompatibleBase(BaseLLMProvider):
    """Base for all OpenAI-format providers (OpenAI, DeepSeek, GLM, Qwen)."""

    BASE_URL: str = ""  # Subclasses must override

    @property
    def provider_name(self) -> str:
        return self.__class__.__name__

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {
                            "type": "object",
                            "properties": {},
                        }),
                    },
                }
                for t in tools
            ]

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self.BASE_URL, json=payload, headers=self._build_headers()
            )
            resp.raise_for_status()
            data = resp.json()

        return self._parse_openai_response(data)

    def _parse_openai_response(self, data: dict) -> ChatResponse:
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage_data = data.get("usage", {})

        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        tool_calls = []
        for tc in message.get("tool_calls", []):
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))

        finish_reason = choice.get("finish_reason", "stop")
        stop_reason_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }

        return ChatResponse(
            content=message.get("content") or None,
            tool_calls=tool_calls,
            usage=usage,
            model=data.get("model", self.model),
            stop_reason=stop_reason_map.get(finish_reason, finish_reason),
        )


class OpenAIProvider(OpenAICompatibleBase):
    """OpenAI GPT API provider."""
    BASE_URL = "https://api.openai.com/v1/chat/completions"

    @property
    def provider_name(self) -> str:
        return "OpenAI"


class DeepSeekProvider(OpenAICompatibleBase):
    """DeepSeek API provider — OpenAI-compatible."""
    BASE_URL = "https://api.deepseek.com/v1/chat/completions"

    @property
    def provider_name(self) -> str:
        return "DeepSeek"


class GLMProvider(OpenAICompatibleBase):
    """Zhipu GLM API provider — OpenAI-compatible."""
    BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    @property
    def provider_name(self) -> str:
        return "GLM"


class QwenProvider(OpenAICompatibleBase):
    """Alibaba Qwen API provider — OpenAI-compatible."""
    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    @property
    def provider_name(self) -> str:
        return "Qwen"


class GenericOpenAICompatible(OpenAICompatibleBase):
    """Generic OpenAI-compatible endpoint (Ollama, vLLM, local models, etc.)."""

    def _get_base_url(self) -> str:
        settings = get_settings()
        base = settings.openai_base_url or "http://localhost:11434/v1"
        return base.rstrip("/") + "/chat/completions"

    @property
    def provider_name(self) -> str:
        return "OpenAI-Compatible"

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        # Override to use dynamic base URL
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {
                            "type": "object",
                            "properties": {},
                        }),
                    },
                }
                for t in tools
            ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self._get_base_url(), json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()

        return self._parse_openai_response(data)


# ────────────────────────────────────────────────────────
# Provider Factory
# ────────────────────────────────────────────────────────

_PROVIDER_MAP: dict[str, type[BaseLLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "glm": GLMProvider,
    "qwen": QwenProvider,
    "openai_compatible": GenericOpenAICompatible,
}


def create_provider() -> BaseLLMProvider:
    """Factory: create the configured LLM provider from settings."""
    settings = get_settings()
    provider_class = _PROVIDER_MAP.get(settings.llm_provider)
    if provider_class is None:
        raise ValueError(
            f"Unknown LLM provider: '{settings.llm_provider}'. "
            f"Available: {list(_PROVIDER_MAP.keys())}"
        )
    return provider_class(
        api_key=settings.api_key,
        model=settings.llm_model,
    )
