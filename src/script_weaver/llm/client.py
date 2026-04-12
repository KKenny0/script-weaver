"""High-level LLM client with convenience methods for agent use."""

from __future__ import annotations

from typing import Any

from script_weaver.core.config import get_settings
from script_weaver.llm.providers import (
    BaseLLMProvider,
    ChatResponse,
    create_provider,
)


class LLMClient:
    """High-level client wrapping the provider with common patterns.

    Agents use this rather than calling providers directly.
    """

    def __init__(self, provider: BaseLLMProvider | None = None):
        self._provider = provider or create_provider()
        self._settings = get_settings()

    @property
    def provider(self) -> BaseLLMProvider:
        return self._provider

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send a chat request with current settings as defaults."""
        return await self._provider.chat(
            messages=messages,
            tools=tools,
            temperature=temperature or self._settings.llm_temperature,
            max_tokens=max_tokens or self._settings.llm_max_tokens,
        )

    async def chat_simple(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        """Convenience: system + user message in one call."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return await self.chat(messages, tools, temperature)
