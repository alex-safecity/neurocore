"""LLM provider protocol and implementations."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM provider implementations.

    Skills that need LLM access declare:
        skill_meta = SkillMeta(..., requires_llm=True)

    NeuroCore injects self.llm during skill init when requires_llm=True.
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...

    # Declared as a plain method returning an AsyncIterator (not ``async def``)
    # so concrete async-generator implementations structurally satisfy it.
    def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]: ...


class AnthropicProvider:
    """Anthropic Claude provider using the anthropic SDK.

    Install: pip install "neurocore-ai[anthropic]"
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            params["system"] = system
        response = await self._client.messages.create(**params)
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        return LLMResponse(
            content=text,
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            params["system"] = system
        async with self._client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIProvider:
    """OpenAI provider using the openai SDK.

    Also serves every OpenAI-compatible gateway (Ollama, vLLM, LM Studio,
    LocalAI, Together, Groq, Fireworks, OpenRouter, custom gateways) by
    passing ``base_url``. ``provider_name`` reflects the configured backend
    so traces and ``requires_llm`` skills report the real provider.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o",
        *,
        base_url: str | None = None,
        provider_name: str = "openai",
    ) -> None:
        import openai

        kwargs: dict[str, Any] = {"api_key": api_key or "not-needed"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = model
        self._provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        api_messages: list[dict[str, str]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend({"role": m.role, "content": m.content} for m in messages)
        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=api_messages,  # type: ignore[arg-type]
        )
        content = response.choices[0].message.content or ""
        return LLMResponse(
            content=content,
            model=self._model,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        api_messages: list[dict[str, str]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend({"role": m.role, "content": m.content} for m in messages)
        stream = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=api_messages,  # type: ignore[arg-type]
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta


class GeminiProvider:
    """Google Gemini provider using the google-genai SDK.

    Install: pip install google-genai>=1.0
    """

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        from google import genai as _genai

        self._client = _genai.Client(api_key=api_key)
        self._model = model

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    def _convert_messages(
        self,
        messages: list[LLMMessage],
        system: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Convert LLMMessage list to Gemini format.

        Returns (contents_list, system_instruction).
        System messages are extracted and merged into system_instruction.
        """
        system_parts: list[str] = []
        if system:
            system_parts.append(system)
        contents: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            elif msg.role == "user":
                contents.append({
                    "role": "user",
                    "parts": [{"text": msg.content}],
                })
            elif msg.role == "assistant":
                contents.append({
                    "role": "model",
                    "parts": [{"text": msg.content}],
                })
        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return contents, system_instruction

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        from google.genai import types as genai_types

        contents, system_instruction = self._convert_messages(messages, system)
        config = genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
            system_instruction=system_instruction,
        )
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        text = response.text or ""
        usage = response.usage_metadata
        return LLMResponse(
            content=text,
            model=self._model,
            input_tokens=usage.prompt_token_count if usage else 0,
            output_tokens=usage.candidates_token_count if usage else 0,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        from google.genai import types as genai_types

        contents, system_instruction = self._convert_messages(messages, system)
        config = genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
            system_instruction=system_instruction,
        )
        async for chunk in self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        ):
            if chunk.text:
                yield chunk.text


class MockProvider:
    """Deterministic mock provider for testing. Never calls a real API."""

    def __init__(self, model: str = "mock-model") -> None:
        self._model = model
        self._responses: list[str] = []
        self.call_count: int = 0
        self.last_messages: list[LLMMessage] = []

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return self._model

    def set_response(self, response: str) -> None:
        """Queue a response to be returned on the next complete() call."""
        self._responses.append(response)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        content = self._responses.pop(0) if self._responses else "mock response"
        return LLMResponse(content=content, model=self._model)

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        response = await self.complete(messages, max_tokens=max_tokens)
        for char in response.content:
            yield char
            await asyncio.sleep(0)


class LiteLLMProvider:
    """Provider backed by LiteLLM, routing to 100+ model APIs.

    Install: pip install "neurocore-ai[litellm]"
    """

    def __init__(
        self, api_key: str = "", model: str = "", *, base_url: str | None = None
    ) -> None:
        import litellm  # noqa: F401  (validate availability eagerly)

        self._api_key = api_key or None
        self._model = model
        self._base_url = base_url

    @property
    def provider_name(self) -> str:
        return "litellm"

    @property
    def model(self) -> str:
        return self._model

    def _build_messages(
        self, messages: list[LLMMessage], system: str | None
    ) -> list[dict[str, str]]:
        api_messages: list[dict[str, str]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend({"role": m.role, "content": m.content} for m in messages)
        return api_messages

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        import litellm

        response = await litellm.acompletion(
            model=self._model,
            messages=self._build_messages(messages, system),
            max_tokens=max_tokens,
            temperature=temperature,
            api_key=self._api_key,
            base_url=self._base_url,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            model=self._model,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 8192,
        temperature: float = 1.0,
        system: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        import litellm

        stream = await litellm.acompletion(
            model=self._model,
            messages=self._build_messages(messages, system),
            max_tokens=max_tokens,
            temperature=temperature,
            api_key=self._api_key,
            base_url=self._base_url,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta


# OpenAI-compatible aliases that ship with sensible default endpoints.
_OPENAI_COMPATIBLE_DEFAULT_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434/v1",
    "vllm": "http://localhost:8000/v1",
}


def build_provider(config: dict[str, Any]) -> LLMProvider | None:
    """Build an LLMProvider from a config dict.

    Keys read:
        llm_provider: provider name (required to build). One of
                      anthropic | openai | openai-compatible | ollama |
                      vllm | litellm | gemini | mock.
        llm_model:    model identifier (optional, uses provider default)
        llm_api_key:  API key (optional for mock/local gateways)
        llm_base_url: endpoint for openai-compatible/ollama/vllm/litellm

    Returns None if llm_provider is not set.
    """
    provider_name = config.get("llm_provider")
    if not provider_name:
        return None
    model = config.get("llm_model", "")
    api_key = config.get("llm_api_key", "")
    base_url = config.get("llm_base_url") or None
    if provider_name == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-sonnet-4-6")
    if provider_name == "openai":
        return OpenAIProvider(api_key=api_key, model=model or "gpt-4o", base_url=base_url)
    if provider_name in ("openai-compatible", "ollama", "vllm"):
        resolved = base_url or _OPENAI_COMPATIBLE_DEFAULT_BASE_URLS.get(provider_name)
        if not resolved:
            raise ValueError(
                f"Provider {provider_name!r} requires a base_url. "
                f"Set llm.base_url in neurocore.yaml (e.g. http://localhost:11434/v1)."
            )
        return OpenAIProvider(
            api_key=api_key,
            model=model,
            base_url=resolved,
            provider_name=provider_name,
        )
    if provider_name == "litellm":
        return LiteLLMProvider(api_key=api_key, model=model, base_url=base_url)
    if provider_name == "gemini":
        return GeminiProvider(api_key=api_key, model=model or "gemini-2.0-flash")
    if provider_name == "mock":
        return MockProvider(model=model or "mock-model")
    raise ValueError(
        f"Unknown llm_provider: {provider_name!r}. Expected: anthropic | openai | "
        f"openai-compatible | ollama | vllm | litellm | gemini | mock"
    )
