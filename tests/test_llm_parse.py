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


def test_split_reply_accepts_typographic_quotes():
    """Qwen3-4B substitutes curly quotes for the ASCII ones the prompt shows."""
    text = 'Nice one. ↪ “I am agree” → “I agree” (drop the "am")'
    _, correction = _split_reply_and_correction(text)
    assert correction is not None
    assert correction["original"] == "I am agree"
    assert correction["corrected"] == "I agree"


def test_split_reply_accepts_bare_arrow_prefix():
    """Observed live: the model opens the line with → instead of ↪."""
    text = 'Good point.\n→ "She have" → "She has" (subject-verb agreement)'
    reply, correction = _split_reply_and_correction(text)
    assert reply == "Good point."
    assert correction == {
        "original": "She have",
        "corrected": "She has",
        "reason": "subject-verb agreement",
    }


def test_split_reply_drops_no_op_correction():
    """Observed live: `"30 years old" → "30 years old" (correct, no error)`.
    A correction of nothing must never reach the learner."""
    text = 'What kind of engineering? ↪ "30 years old" → "30 years old" (correct, no error)'
    reply, correction = _split_reply_and_correction(text)
    assert correction is None
    assert reply == "What kind of engineering?"


def test_split_reply_strips_unparseable_marker_from_spoken_text():
    """Observed live: a marker with only the corrected half. It yields no
    correction, but must not survive into the reply — the TTS speaks it."""
    text = (
        "That's a nice observation!\n\n"
        '→ "The people in my country are very friendly." (subject-verb agreement)\n\n'
        "What traditions do you have there?"
    )
    reply, correction = _split_reply_and_correction(text)
    assert correction is None
    assert "→" not in reply
    assert "subject-verb agreement" not in reply
    assert reply == "That's a nice observation!\n\nWhat traditions do you have there?"


def test_split_reply_strips_explicit_none_token():
    """The prompt requires a ↪ line on every turn so the model has an explicit
    way to say "no error"; `↪ none` must never be spoken to the learner."""
    reply, correction = _split_reply_and_correction(
        "That sounds like a great trip! Where did you go?\n↪ none"
    )
    assert correction is None
    assert reply == "That sounds like a great trip! Where did you go?"


def test_split_reply_missing_reason_still_parses():
    text = 'Sure. ↪ "I go" → "I went"'
    _, correction = _split_reply_and_correction(text)
    assert correction == {"original": "I go", "corrected": "I went", "reason": ""}
