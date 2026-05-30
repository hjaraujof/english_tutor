"""Route-level tests: text review path, audio upload extension coercion (F2),
and orphan-file cleanup on ASR failure (F3). ASR/LLM are stubbed in conftest."""
from __future__ import annotations

from pathlib import Path


def _audio_dir(test_client) -> Path:
    return test_client.app.state.config.server.data_dir / "audio"


def test_review_text_returns_review_and_persists(client):
    response = client.post("/api/review", json={"text": "He go to school yesterday."})
    assert response.status_code == 200
    body = response.json()
    assert "review" in body
    assert "metrics" in body
    assert "id" in body

    sessions = client.app.state.db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["kind"] == "text"
    assert sessions[0]["transcript"] == "He go to school yesterday."


def test_upload_garbage_extension_is_coerced_to_webm(client):
    garbage_name = "evil\n" + "x" * 500 + ".exe@#$"
    response = client.post(
        "/api/sessions",
        files={"audio": (garbage_name, b"fake-audio-bytes", "application/octet-stream")},
    )
    assert response.status_code == 200

    stored = list(_audio_dir(client).iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".webm"
    assert not stored[0].name.endswith(".part")


def test_known_extension_is_preserved(client):
    response = client.post(
        "/api/sessions",
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )
    assert response.status_code == 200
    stored = list(_audio_dir(client).iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".wav"


def test_asr_failure_leaves_no_orphan_file(raising_client):
    response = raising_client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 500

    remaining = list(_audio_dir(raising_client).iterdir())
    assert remaining == []

    sessions = raising_client.app.state.db.list_sessions()
    assert sessions == []
