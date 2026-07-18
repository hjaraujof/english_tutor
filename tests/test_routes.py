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


def test_llm_failure_after_transcription_leaves_no_orphan(llm_down_client):
    response = llm_down_client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 503
    assert "llama-server" in response.json()["detail"]
    assert list(_audio_dir(llm_down_client).iterdir()) == []
    assert llm_down_client.app.state.db.list_sessions() == []


def test_empty_transcription_skips_llm_and_persists(empty_asr_client):
    response = empty_asr_client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 200
    assert "No speech detected" in response.json()["review"]["overall"]["summary"]
    sessions = empty_asr_client.app.state.db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["kind"] == "audio"


def test_upload_write_failure_returns_400_and_cleans_up(client, monkeypatch):
    def raising_copy(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr("backend.routes.sessions.shutil.copyfileobj", raising_copy)
    response = client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 400
    assert list(_audio_dir(client).iterdir()) == []


def test_blocking_asr_and_db_run_off_event_loop(client):
    import asyncio

    state = client.app.state
    observed = {}
    original_insert = state.db.insert_session

    def probed_insert(**kwargs):
        try:
            asyncio.get_running_loop()
            observed["db_on_loop"] = True
        except RuntimeError:
            observed["db_on_loop"] = False
        return original_insert(**kwargs)

    state.db.insert_session = probed_insert
    response = client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 200
    assert state.asr.saw_running_loop is False
    assert observed["db_on_loop"] is False


def test_session_audio_roundtrip_and_404s(client):
    upload = client.post(
        "/api/sessions",
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )
    audio_id = upload.json()["id"]
    text = client.post("/api/review", json={"text": "He go to school."})
    text_id = text.json()["id"]

    sessions = {session["id"]: session for session in client.get("/api/sessions").json()["sessions"]}
    assert sessions[audio_id]["has_audio"] is True
    assert sessions[text_id]["has_audio"] is False

    audio_response = client.get(f"/api/sessions/{audio_id}/audio")
    assert audio_response.status_code == 200
    assert audio_response.content == b"fake-audio-bytes"
    assert audio_response.headers["content-type"].startswith("audio/wav")

    assert client.get(f"/api/sessions/{text_id}/audio").status_code == 404
    assert client.get("/api/sessions/99999/audio").status_code == 404


def test_wpm_trend_excludes_text_sessions(client):
    client.post("/api/review", json={"text": "He go to school yesterday."})
    client.post(
        "/api/sessions",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    wpm = client.get("/api/sessions/trend/words_per_minute").json()["points"]
    assert len(wpm) == 1

    filler = client.get("/api/sessions/trend/filler_ratio").json()["points"]
    assert len(filler) == 2
