"""Adapt Stagehand's LLM callback protocol to any OpenAI-compatible endpoint.

Stagehand v4 drives act/extract through an injected LLM. Tortoise points it
at local Ollama by default (free, keeps the crawl at about zero cost) but any
OpenAI-compatible endpoint works — including Ultramarine. This adapter is the
single seam between Stagehand's callback types and the OpenAI SDK.

Two protocols live here:

* **Data calls** — Stagehand sends the requested extraction schema in
  ``response_format``; the callback returns ``structured_content`` shaped like
  that schema. Small local models add protocol chatter (the extraction
  envelope keys) or drop fields, so the adapter conforms the reply to the
  schema's properties, unwrapping Stagehand's ``extraction_response`` wrapper
  when present and filling schema defaults for anything missing.
* **Metadata calls** — Stagehand's chunking protocol asks for
  ``{"progress": str, "completed": bool}``; the same conformance path handles
  it, because it is driven by whatever schema the call carries.
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

from ..cost import usage_from_response
from ..models import TokenUsage

logger = logging.getLogger(__name__)

# Keys Stagehand's own extraction protocol may add around the data; the real
# data hides under "extraction_response" when the model echoes the envelope.
_PROTOCOL_KEYS = ("instruction", "extraction_response", "chunksTotal", "chunksSeen")


class OpenAICompatLLM:
    """The LLM Stagehand expects, backed by an OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)
        # Running token total across every call this adapter made, so the
        # activity can log what driving the browser cost.
        self.usage = TokenUsage()
        self.calls = 0

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
        counted = usage_from_response(response)
        self.calls += 1
        self.usage = TokenUsage(
            input_tokens=self.usage.input_tokens + counted.input_tokens,
            cached_tokens=self.usage.cached_tokens + counted.cached_tokens,
            output_tokens=self.usage.output_tokens + counted.output_tokens,
        )
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
            parsed = _parse_json_object(text)
            conformed = self._conform_to_schema(parsed, getattr(params, "response_format", None))
            return LLMStructuredGenerateResult(
                **common,
                output_format="json_schema",
                structured_content=conformed,
            )
        return LLMMessageGenerateResult(**common, output_format="text")

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

    def _conform_to_schema(self, parsed: dict | None, response_format) -> dict:
        """Shape the model's JSON into whatever ``response_format`` asks for.

        Driven entirely by the schema the call carries, so it works for both
        data extraction and metadata calls without special cases.
        """
        schema = _schema_of(response_format)
        properties = _unwrap_root(schema.get("properties")) if schema else None
        if not isinstance(properties, dict):
            return parsed if isinstance(parsed, dict) else {}

        data: object = parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("extraction_response"), (dict, list)):
            data = parsed["extraction_response"]

        conformed: dict = {}
        if isinstance(data, dict):
            mapping = _match_keys(properties, data)
        else:
            mapping = {}
        for name, prop in properties.items():
            source = mapping.get(name)
            if source is not None:
                conformed[name] = data[source]
            else:
                conformed[name] = _value_for(name, _unwrap_root(prop), data)
        return conformed


def _unwrap_root(value):
    """Recursively unwrap pydantic root models (``FieldSchemaN(root=...)``)."""
    root = getattr(value, "root", None)
    if root is not None:
        return _unwrap_root(root)
    if isinstance(value, dict):
        return {key: _unwrap_root(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_root(item) for item in value]
    return value


def _norm(key: str) -> str:
    """Lowercase letters and digits only, for tolerant key comparison."""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _match_keys(properties: dict, data: dict) -> dict:
    """Map schema property names to the model's actual keys, fuzzily.

    Small local models invent key names ("page_title" for "title",
    "main_heading" for "heading"); match by normalised containment, closest
    length first, so their data still lands in the right fields.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    normalised = {key: _norm(key) for key in data}
    for name in properties:
        target = _norm(name)
        if not target:
            continue
        if name in data:
            mapping[name] = name
            used.add(name)
            continue
        candidates = [
            key
            for key in data
            if key not in used and (target in normalised[key] or normalised[key] in target)
        ]
        if candidates:
            best = min(candidates, key=lambda key: abs(len(normalised[key]) - len(target)))
            mapping[name] = best
            used.add(best)
    return mapping


def _schema_of(response_format) -> dict:
    """The JSON schema carried by a structured-response format, if any.

    ``schema_`` is a pydantic root model wrapping the raw schema dict.
    """
    schema = getattr(response_format, "schema_", None)
    if schema is None and isinstance(response_format, dict):
        schema = response_format.get("schema_") or response_format.get("schema")
    unwrapped = _unwrap_root(schema)
    return unwrapped if isinstance(unwrapped, dict) else {}


def _value_for(name: str, prop: dict, data: object):
    """One schema property's value: from the model's data, else a default.

    A value of the wrong shape (a small model's common mistake — e.g. a
    string where the schema wants an object) is treated as absent, so the
    schema default keeps the reply protocol-valid instead of crashing the
    caller's validation.
    """
    if isinstance(data, dict) and name in data and _matches_type(data[name], prop):
        return data[name]
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], (dict, list)):
        # A model returning a bare list for an object schema.
        return _value_for(name, prop, data[0])
    if "default" in prop:
        return prop["default"]
    # Unions ({"anyOf": [...]}) — prefer the first non-null branch.
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        for branch in any_of:
            branch = branch if isinstance(branch, dict) else {}
            branch_type = branch.get("type")
            if branch_type and branch_type != "null":
                return _default_for_type(branch_type, branch)
        return None
    return _default_for_type(prop.get("type"), prop)


def _matches_type(value: object, prop: dict) -> bool:
    """Does this value fit the property's (possibly union) type?"""
    if not isinstance(prop, dict):
        return True
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return any(
            isinstance(branch, dict) and _matches_type(value, branch) for branch in any_of
        )
    prop_type = prop.get("type")
    types = set(prop_type if isinstance(prop_type, list) else [prop_type])
    if "object" in types:
        return isinstance(value, dict)
    if "array" in types:
        return isinstance(value, list)
    if "boolean" in types:
        return isinstance(value, bool)
    if "integer" in types:
        return isinstance(value, int) and not isinstance(value, bool)
    if "number" in types:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if "string" in types:
        return isinstance(value, str)
    if "null" in types:
        return value is None
    return True  # untyped property: accept


def _default_for_type(prop_type, prop: dict):
    types = prop_type if isinstance(prop_type, list) else [prop_type]
    if "object" in types:
        return {}
    if "array" in types:
        return []
    if "boolean" in types:
        return False
    if "integer" in types or "number" in types:
        return 0
    if "string" in types:
        return ""
    if "null" in types:
        return None
    return ""


def _parse_json_object(text: str) -> dict | None:
    """Best-effort parse of a model reply into a JSON object, fences and all."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            line for line in cleaned.splitlines() if not line.strip().startswith("```")
        ).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            logger.warning("stagehand LLM reply was not JSON; extract may retry")
            return None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("stagehand LLM reply had unparseable JSON; extract may retry")
            return None
    return parsed if isinstance(parsed, dict) else None


def _message_text(content) -> str:
    """Flatten Stagehand message content to plain text.

    The content block is a *union root model*: the actual ``type``/``text``
    fields sit one root-wrap down (``_unwrap_root``), and reading
    ``block.text`` directly silently drops the page DOM — the model then
    receives an empty message and hallucinates instead of extracting.
    """
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        block = _unwrap_root(block)
        text = getattr(block, "text", None)
        if text is None:
            extra = getattr(block, "model_extra", None) or {}
            text = extra.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts)
