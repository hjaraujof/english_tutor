"""Database tests against real SQLite on tmp_path. Covers round-trip, correction
fan-out, FK cascade, uncapped ascending trend (F5), and recurring-error counts."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.db import Database


def _review(corrections=None) -> dict:
    return {
        "corrections": corrections or [],
        "fluency_notes": [],
        "vocabulary_suggestions": [],
        "overall": {"summary": "ok", "strengths": [], "next_focus": []},
    }


def _metrics(words_per_minute: float) -> dict:
    return {
        "word_count": 10,
        "unique_words": 8,
        "duration_seconds": 5.0,
        "words_per_minute": words_per_minute,
        "type_token_ratio": 0.8,
        "filler_count": 0,
        "filler_ratio": 0.0,
        "mean_segment_words": 5.0,
        "longest_pause_seconds": 0.5,
    }


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "t.db")


def test_insert_and_list_round_trip(db: Database):
    session_id = db.insert_session(
        kind="text",
        transcript="hello world",
        review=_review(),
        metrics=_metrics(120.0),
    )
    assert session_id == 1
    sessions = db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["transcript"] == "hello world"
    assert sessions[0]["metrics"]["words_per_minute"] == 120.0


def test_corrections_fan_out_into_errors(db: Database):
    corrections = [
        {"type": "tense", "original": "go", "corrected": "went", "explanation": "past", "severity": "high"},
        {"type": "agreement", "original": "he go", "corrected": "he goes", "explanation": "3sg", "severity": "medium"},
    ]
    db.insert_session(kind="text", transcript="x", review=_review(corrections), metrics=_metrics(100.0))
    recurring = db.recurring_error_types(lookback_days=30, min_count=1)
    types = {row["type"]: row["count"] for row in recurring}
    assert types == {"tense": 1, "agreement": 1}


def test_fk_cascade_deletes_child_errors(db: Database):
    corrections = [
        {"type": "tense", "original": "go", "corrected": "went", "explanation": "past", "severity": "high"},
    ]
    session_id = db.insert_session(kind="text", transcript="x", review=_review(corrections), metrics=_metrics(100.0))

    with db._connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) AS c FROM errors WHERE session_id = ?", (session_id,)
        ).fetchone()["c"]
        assert before == 1
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    with db._connect() as connection:
        after = connection.execute(
            "SELECT COUNT(*) AS c FROM errors WHERE session_id = ?", (session_id,)
        ).fetchone()["c"]
    assert after == 0


def test_metric_trend_uncapped_and_ascending(db: Database):
    for index in range(60):
        db.insert_session(
            kind="text",
            transcript=f"session {index}",
            review=_review(),
            metrics=_metrics(float(index)),
        )

    trend = db.metric_trend("words_per_minute")
    assert len(trend) == 60
    ids = [point["id"] for point in trend]
    assert ids == sorted(ids)
    values = [point["value"] for point in trend]
    assert values == [float(index) for index in range(60)]


def test_recurring_error_threshold(db: Database):
    correction = {"type": "tense", "original": "go", "corrected": "went", "explanation": "past", "severity": "high"}
    for _ in range(3):
        db.insert_session(kind="text", transcript="x", review=_review([correction]), metrics=_metrics(100.0))

    assert db.recurring_error_types(lookback_days=30, min_count=3) == [{"type": "tense", "count": 3}]
    assert db.recurring_error_types(lookback_days=30, min_count=4) == []
