"""Adapt Stagehand's LLM callback protocol to any OpenAI-compatible endpoint.

Stagehand v4 drives act/extract through an injected LLM. Tortoise points it
at local Ollama by default (free, keeps the crawl at about zero cost) but any
OpenAI-compatible endpoint works — including Ultramarine. This adapter is the
single seam between Stagehand's callback types and the OpenAI SDK.
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI
from stagehand import LLMMessageGenerateResult, LLMTextContent, LLMUsage
from stagehand._generated.models import (
    LLMMessageGenerateParams,
    LLMStructuredGenerateParams,
    LLMStructuredGenerateResult,
)

logger = logging.getLogger(__name__)


class OpenAICompatLLM:
    """The ``ClientLLM`` Stagehand expects, backed by an OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)

    async def generate(
        self,
        params: LLMMessageGenerateParams | LLMStructuredGenerateParams,
    ) -> LLMMessageGenerateResult | LLMStructuredGenerateResult:
        messages = self._to_openai_messages(params)
        wants_json = bool(getattr(params, "response_format", None))

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=getattr(params, "temperature", 0.0) or 0.0,
            response_format={"type": "json_object"} if wants_json else None,
        )
        text = response.choices[0].message.content or ""
        usage = LLMUsage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(response.usage, "total_tokens", 0) or 0,
        )

        common = {
            "role": "assistant",
            "content": LLMTextContent(type="text", text=text),
            "stop_reason": "end_turn",
            "usage": usage,
        }
        if isinstance(params, LLMStructuredGenerateParams):
            return LLMStructuredGenerateResult(
                **common,
                output_format=getattr(params, "response_format", None),
                structured_content=self._parse_json(text),
            )
        return LLMMessageGenerateResult(**common)

    @staticmethod
    def _to_openai_messages(params) -> list[dict]:
        messages: list[dict] = []
        system_prompt = getattr(params, "system_prompt", None)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for message in params.messages:
            text = _message_text(message.content)
            if text:
                messages.append({"role": str(message.role.value), "content": text})
        return messages

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """Best-effort parse of the model reply into structured content."""
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            logger.warning("stagehand LLM reply was not JSON; extract may retry")
            return None


def _message_text(content) -> str:
    """Flatten Stagehand message content (a block or list of blocks) to text."""
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)
