"""Live-socket tests: history-trim boundary, malformed-frame guards, and
server-side re-chunking. The WS test uses the real silero VAD (phase2 extra)
over the TestClient WebSocket; no LLM turn is triggered because the frames
contain no speech."""
from __future__ import annotations

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from backend.routes.live import MAX_HISTORY_TURNS, _trim_history


def _fake_history(pairs: int) -> list[dict[str, str]]:
    history = [{"role": "system", "content": "sys"}]
    for index in range(pairs):
        history.append({"role": "user", "content": f"u{index}"})
        history.append({"role": "assistant", "content": f"a{index}"})
    return history


def test_trim_history_noop_at_cap():
    history = _fake_history(MAX_HISTORY_TURNS)
    assert _trim_history(history) is history


def test_trim_history_preserves_system_message_and_complete_pairs():
    history = _fake_history(MAX_HISTORY_TURNS + 1)
    trimmed = _trim_history(history)
    assert len(trimmed) == 1 + 2 * MAX_HISTORY_TURNS
    assert trimmed[0]["role"] == "system"
    assert trimmed[1]["role"] == "user"
    assert trimmed[-1]["role"] == "assistant"


def test_live_socket_survives_malformed_frames(client):
    pytest.importorskip("silero_vad")
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_text("this is not json")  # F4: malformed text frame
        websocket.send_bytes(b"\x01\x02\x03")  # odd byte count
        websocket.send_bytes(np.zeros(100, dtype=np.int16).tobytes())  # non-VAD-window size
        websocket.send_bytes(np.zeros(700, dtype=np.int16).tobytes())  # re-chunked across sends
        websocket.send_text('{"type": "end"}')
        # A clean disconnect (no {"type": "error"} frame first) proves every
        # malformed frame was skipped rather than killing the socket.
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()


class FakeVADIterator:
    """Emits speech start on the first frame and end on the second, so two
    silent frames drive one full conversational turn."""

    def __init__(self, model, sampling_rate, min_silence_duration_ms):
        self.calls = 0

    def __call__(self, frame):
        self.calls += 1
        if self.calls == 1:
            return {"start": 0}
        if self.calls == 2:
            return {"end": 1024}
        return None


def test_live_turn_streams_and_persists_session(client, monkeypatch):
    silero = pytest.importorskip("silero_vad")
    import json

    monkeypatch.setattr(silero, "load_silero_vad", lambda: None)
    monkeypatch.setattr(silero, "VADIterator", FakeVADIterator)

    frame = np.zeros(512, dtype=np.int16).tobytes()
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        events = [json.loads(websocket.receive_text()) for _ in range(5)]
        assert [event["type"] for event in events] == [
            "transcript", "reply_delta", "reply_delta", "reply_end", "latency",
        ]
        assert events[0]["text"] == "I goes to the store yesterday."
        websocket.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    sessions = client.app.state.db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["kind"] == "live"
    assert "store" in sessions[0]["transcript"]
    assert sessions[0]["metrics"]["word_count"] == 6


def test_second_concurrent_live_connection_is_rejected(client):
    pytest.importorskip("silero_vad")
    import json

    with client.websocket_connect("/ws/live") as first:
        with client.websocket_connect("/ws/live") as second:
            payload = json.loads(second.receive_text())
            assert payload["type"] == "error"
            assert "already active" in payload["detail"]
        first.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            first.receive_text()
