"""Unit tests for fluency metrics. No LLM, no network — pure math on synthetic data."""
from __future__ import annotations

from backend.analysis import compute_metrics, tokenize


def test_tokenize_strips_punctuation():
    tokens = tokenize("Hello, world! It's me.")
    assert tokens == ["hello", "world", "it's", "me"]


def test_compute_metrics_text_only():
    text = "I went to the store and bought some milk."
    metrics = compute_metrics(text=text, duration_seconds=0.0, segments=None)
    assert metrics.word_count == 9
    assert metrics.unique_words == 9
    assert metrics.type_token_ratio == 1.0
    assert metrics.filler_count == 0
    assert metrics.words_per_minute == 0.0
    assert metrics.longest_pause_seconds == 0.0


def test_compute_metrics_words_per_minute():
    text = "one two three four five six"
    metrics = compute_metrics(text=text, duration_seconds=12.0, segments=None)
    assert metrics.word_count == 6
    assert metrics.words_per_minute == 30.0


def test_filler_count_counts_um_and_phrases():
    text = "Um, I went to the store, you know, and like bought milk."
    metrics = compute_metrics(text=text, duration_seconds=10.0, segments=None)
    assert metrics.filler_count >= 3
    assert metrics.filler_ratio > 0


def test_type_token_ratio_drops_with_repetition():
    text = "the the the the the cat sat"
    metrics = compute_metrics(text=text, duration_seconds=5.0, segments=None)
    assert metrics.unique_words == 3
    assert metrics.word_count == 7
    assert metrics.type_token_ratio < 0.5


def test_longest_pause_from_segments():
    segments = [
        {"text": "First.", "start": 0.0, "end": 1.0},
        {"text": "Second.", "start": 4.5, "end": 5.0},
        {"text": "Third.", "start": 5.5, "end": 6.0},
    ]
    metrics = compute_metrics(
        text="First. Second. Third.",
        duration_seconds=6.0,
        segments=segments,
    )
    assert abs(metrics.longest_pause_seconds - 3.5) < 1e-6
    assert metrics.mean_segment_words == 1.0


def test_empty_text():
    metrics = compute_metrics(text="", duration_seconds=10.0, segments=None)
    assert metrics.word_count == 0
    assert metrics.type_token_ratio == 0.0
    assert metrics.filler_ratio == 0.0
    assert metrics.words_per_minute == 0.0
