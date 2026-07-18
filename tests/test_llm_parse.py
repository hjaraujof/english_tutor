"""No-network tests for the two parsing safety branches: the LLM JSON fallback
and the live correction-marker splitter."""
from __future__ import annotations

from pathlib import Path

from backend.llm import LLMClient
from backend.routes.live import _split_reply_and_correction


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "backend" / "prompts"


def _client() -> LLMClient:
    return LLMClient(base_url="http://127.0.0.1:8080", model="test-model", prompts_dir=PROMPTS_DIR)


def test_parse_review_falls_back_on_non_json():
    review = _client()._parse_review("not json at all")
    assert review.corrections == []
    assert "could not be parsed" in review.overall.summary


def test_parse_review_falls_back_on_null_content():
    review = _client()._parse_review(None)
    assert review.corrections == []
    assert "could not be parsed" in review.overall.summary


def test_parse_review_validates_good_json():
    payload = (
        '{"corrections": [], "fluency_notes": [], "vocabulary_suggestions": [], '
        '"overall": {"summary": "great", "strengths": [], "next_focus": []}}'
    )
    review = _client()._parse_review(payload)
    assert review.overall.summary == "great"


def test_split_reply_extracts_correction():
    text = 'That sounds fun. ↪ "I goes" → "I go" (subject-verb agreement)'
    reply, correction = _split_reply_and_correction(text)
    assert reply == "That sounds fun."
    assert correction == {
        "original": "I goes",
        "corrected": "I go",
        "reason": "subject-verb agreement",
    }


def test_split_reply_plain_text_has_no_correction():
    reply, correction = _split_reply_and_correction("  Just a normal reply.  ")
    assert reply == "Just a normal reply."
    assert correction is None
