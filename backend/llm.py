"""Client for llama-server (OpenAI-compat). Renders prompts, calls /v1/chat/completions
with JSON-schema structured output, parses into pydantic models.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class Correction(BaseModel):
    type: str
    original: str
    corrected: str
    explanation: str
    severity: str = "medium"


class FluencyNote(BaseModel):
    observation: str
    suggestion: str


class VocabSuggestion(BaseModel):
    model_config = {"populate_by_name": True}

    phrase: str
    alternative: str
    register_level: str = Field(default="neutral", alias="register")


class OverallFeedback(BaseModel):
    summary: str
    estimated_cefr: str | None = None
    strengths: list[str] = []
    next_focus: list[str] = []


class Review(BaseModel):
    corrections: list[Correction] = []
    fluency_notes: list[FluencyNote] = []
    vocabulary_suggestions: list[VocabSuggestion] = []
    overall: OverallFeedback


REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "original": {"type": "string"},
                    "corrected": {"type": "string"},
                    "explanation": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["type", "original", "corrected", "explanation"],
            },
        },
        "fluency_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["observation", "suggestion"],
            },
        },
        "vocabulary_suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "alternative": {"type": "string"},
                    "register": {"type": "string"},
                },
                "required": ["phrase", "alternative"],
            },
        },
        "overall": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "estimated_cefr": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "next_focus": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
    "required": ["corrections", "fluency_notes", "vocabulary_suggestions", "overall"],
}


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        prompts_dir: Path,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompts_dir = prompts_dir
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _load_prompt(self, name: str) -> str:
        return (self.prompts_dir / name).read_text(encoding="utf-8")

    async def review(self, transcript: str, native_language: str, cefr_level: str) -> Review:
        prompt = self._load_prompt("grammar_review.md").format(
            transcript=transcript,
            native_language=native_language,
            cefr_level=cefr_level,
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You output ONLY a JSON object as specified. No prose."},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "review", "strict": True, "schema": REVIEW_JSON_SCHEMA},
            },
        }
        response = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return self._parse_review(content)

    def _parse_review(self, content: str) -> Review:
        try:
            data = json.loads(content)
            return Review.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("review parse failed (%s); raw=%s", exc, content[:500])
            return Review(
                overall=OverallFeedback(
                    summary="The model returned a response that could not be parsed. Please try again.",
                )
            )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
    ):
        """Yields content deltas from llama-server streaming chat completion."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        async with self._client.stream(
            "POST", f"{self.base_url}/v1/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta
