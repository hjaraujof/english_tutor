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

from backend.llm import LLMClient

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
        model="Qwen2.5-3B-Instruct",
        prompts_dir=PROMPTS_DIR,
    )
    try:
        review = await client.review(
            transcript="He go to school yesterday.",
            native_language="Spanish",
            cefr_level="B1",
        )
        types = {item.type for item in review.corrections}
        assert review.corrections, "expected at least one correction"
        assert types & {"tense", "grammar", "agreement"}
    finally:
        await client.aclose()


@pytest_skip_unless_server
@pytest.mark.asyncio
async def test_review_clean_input_yields_few_or_no_errors():
    client = LLMClient(
        base_url=BASE_URL,
        model="Qwen2.5-3B-Instruct",
        prompts_dir=PROMPTS_DIR,
    )
    try:
        review = await client.review(
            transcript="I went to the bookstore yesterday and bought a novel.",
            native_language="Spanish",
            cefr_level="B2",
        )
        assert len(review.corrections) <= 1
        assert review.overall.summary
    finally:
        await client.aclose()
