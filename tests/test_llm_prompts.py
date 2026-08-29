"""Integration tests against a running llama-server. Skipped automatically if
the server isn't reachable; run manually with `uv run pytest -m integration`.

The point is to sanity-check that the prompt + structured-output schema
produces sensible, parseable corrections for known-bad inputs.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from backend.llm import CORRECTION_TYPES, LLMClient

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("ENGLISH_TUTOR_LLM_URL", "http://127.0.0.1:8080")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "backend" / "prompts"


def _server_up() -> bool:
    try:
        response = httpx.get(f"{BASE_URL}/health", timeout=1.0)
        return response.status_code == 200
    except Exception:
        return False


pytest_skip_unless_server = pytest.mark.skipif(not _server_up(), reason="llama-server not running")


@pytest_skip_unless_server
@pytest.mark.asyncio
async def test_review_flags_subject_verb_agreement():
    client = LLMClient(
        base_url=BASE_URL,
        model="Qwen3-4B-Instruct-2507",
        prompts_dir=PROMPTS_DIR,
    )
    try:
        review = await client.review(
            transcript="He go to school yesterday.",
            native_language="Spanish",
            cefr_level="B1",
        )
        assert review.corrections, "expected at least one correction"
        # A 3B model's exact label is noisy; the durable invariants are that it
        # LOCATED the error and stayed inside the schema-enforced taxonomy.
        assert any("go" in item.original for item in review.corrections)
        assert {item.type for item in review.corrections} <= set(CORRECTION_TYPES)
    finally:
        await client.aclose()


@pytest_skip_unless_server
@pytest.mark.asyncio
async def test_review_clean_input_yields_few_or_no_errors():
    client = LLMClient(
        base_url=BASE_URL,
        model="Qwen3-4B-Instruct-2507",
        prompts_dir=PROMPTS_DIR,
    )
    try:
        review = await client.review(
            transcript="I went to the bookstore yesterday and bought a novel.",
            native_language="Spanish",
            cefr_level="B2",
        )
        # Leaving correct English alone is the invariant that separates a usable
        # tutor from a noisy one, so assert zero rather than a tolerance. Qwen
        # 2.5 3B failed this on every clean sentence measured; Qwen 3 4B with
        # the current prompt passed all of them.
        assert review.corrections == []
        assert review.overall.summary
    finally:
        await client.aclose()
