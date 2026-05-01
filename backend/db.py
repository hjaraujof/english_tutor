"""SQLite persistence: sessions, errors per session, fluency metrics over time."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- 'audio' | 'text' | 'live'
    audio_path TEXT,
    transcript TEXT NOT NULL,
    review_json TEXT NOT NULL,          -- full Review pydantic dump
    metrics_json TEXT NOT NULL          -- FluencyMetrics dump
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    original TEXT NOT NULL,
    corrected TEXT NOT NULL,
    explanation TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium'
);

CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(type);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at DESC);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def insert_session(
        self,
        *,
        kind: str,
        transcript: str,
        review: dict[str, Any],
        metrics: dict[str, Any],
        audio_path: Path | None = None,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions (created_at, kind, audio_path, transcript, review_json, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    kind,
                    str(audio_path) if audio_path else None,
                    transcript,
                    json.dumps(review),
                    json.dumps(metrics),
                ),
            )
            session_id = cursor.lastrowid
            assert session_id is not None
            corrections = review.get("corrections", []) or []
            connection.executemany(
                """
                INSERT INTO errors (session_id, type, original, corrected, explanation, severity)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        item.get("type", "grammar"),
                        item.get("original", ""),
                        item.get("corrected", ""),
                        item.get("explanation", ""),
                        item.get("severity", "medium"),
                    )
                    for item in corrections
                ],
            )
            return int(session_id)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, created_at, kind, transcript, review_json, metrics_json "
                "FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "kind": row["kind"],
                    "transcript": row["transcript"],
                    "review": json.loads(row["review_json"]),
                    "metrics": json.loads(row["metrics_json"]),
                }
            )
        return result

    def recurring_error_types(self, lookback_days: int = 30, min_count: int = 3) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT errors.type AS type, COUNT(*) AS count
                FROM errors
                JOIN sessions ON sessions.id = errors.session_id
                WHERE sessions.created_at >= datetime('now', ?)
                GROUP BY errors.type
                HAVING count >= ?
                ORDER BY count DESC
                """,
                (f'-{int(lookback_days)} days', min_count),
            ).fetchall()
        return [{"type": row["type"], "count": row["count"]} for row in rows]

    def metric_trend(self, key: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, created_at, metrics_json FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        trend = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            if key in metrics:
                trend.append({"id": row["id"], "created_at": row["created_at"], "value": metrics[key]})
        trend.reverse()
        return trend
