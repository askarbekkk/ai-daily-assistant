"""One interface for structured (Pydantic) outputs over OpenAI or Gemini."""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel

from ..config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    """LLM_PROVIDER=none, missing key, or the provider call failed."""


class LLMEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider = settings.llm_provider.lower()
        self._client = None

    @property
    def enabled(self) -> bool:
        if self.provider == "openai":
            return bool(self.settings.openai_api_key)
        if self.provider == "gemini":
            return bool(self.settings.gemini_api_key)
        return False

    async def structured(self, system: str, user: str, schema: type[T]) -> T:
        if not self.enabled:
            raise LLMUnavailable(f"LLM provider '{self.provider}' is not configured")
        try:
            if self.provider == "openai":
                return await self._openai(system, user, schema)
            return await self._gemini(system, user, schema)
        except LLMUnavailable:
            raise
        except Exception as exc:
            log.warning("LLM call failed: %s", exc)
            raise LLMUnavailable(f"{self.provider} call failed: {exc}") from exc

    async def _openai(self, system: str, user: str, schema: type[T]) -> T:
        from openai import AsyncOpenAI

        self._client = self._client or AsyncOpenAI(api_key=self.settings.openai_api_key)
        completion = await self._client.chat.completions.parse(
            model=self.settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            temperature=0.3,
        )
        message = completion.choices[0].message
        if message.parsed is None:
            raise LLMUnavailable(f"OpenAI refused or returned no parsed output: {message.refusal}")
        return message.parsed

    async def _gemini(self, system: str, user: str, schema: type[T]) -> T:
        from google import genai
        from google.genai import types

        from google.genai.errors import APIError

        self._client = self._client or genai.Client(api_key=self.settings.gemini_api_key)
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.3,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        models = [self.settings.gemini_model, *_split(self.settings.gemini_fallback_models)]
        for i, model in enumerate(models):
            try:
                response = await self._client.aio.models.generate_content(model=model, contents=user, config=config)
            except APIError as exc:
                if exc.code in (429, 503) and i < len(models) - 1:
                    log.warning("Gemini %s unavailable (%s), trying %s", model, exc.code, models[i + 1])
                    continue
                raise
            if isinstance(response.parsed, schema):
                return response.parsed
            return schema.model_validate_json(response.text or "")
        raise LLMUnavailable("no Gemini model configured")


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]
