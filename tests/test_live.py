"""Live-socket tests: token-budget history trim, malformed-frame guards,
server-side re-chunking, reply interruption and the end-of-session review. The
malformed-frame test uses the real silero VAD (phase2 extra) over the
TestClient WebSocket; no LLM turn is triggered because the frames contain no
speech."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from backend.asr import Transcription
from backend.llm import Correction, OverallFeedback, Review
from backend.routes import live as live_routes
from backend.routes.live import MESSAGE_OVERHEAD_TOKENS, _Conversation, _trim_history
from tests.conftest import FakeASR, FakeLLM


def _fake_history(pairs: int) -> list[dict[str, str]]:
    """System message, `pairs` complete turns, then the newest user message."""
    history = [{"role": "system", "content": "sys"}]
    for index in range(pairs):
        history.append({"role": "user", "content": f"u{index}"})
        history.append({"role": "assistant", "content": f"a{index}"})
    history.append({"role": "user", "content": "newest"})
    return history


def _cost(message_count: int, tokens_each: int) -> int:
    return message_count * (tokens_each + MESSAGE_OVERHEAD_TOKENS)


def test_trim_history_keeps_everything_that_fits():
    history = _fake_history(3)
    counts = [100] * len(history)
    trimmed, trimmed_counts = _trim_history(history, counts, _cost(len(history), 100))
    assert trimmed == history
    assert trimmed_counts == counts


def test_trim_history_drops_oldest_turns_by_tokens_not_by_count():
    # Three turns of 250-token replies: a count of turns calls this short, the
    # token budget does not.
    history = _fake_history(3)
    counts = [100] + [20, 250] * 3 + [20]
    budget = 100 + 20 + 250 + 20 + _cost(4, 0)  # system + one turn + newest
    trimmed, trimmed_counts = _trim_history(history, counts, budget)
    assert [message["content"] for message in trimmed] == ["sys", "u2", "a2", "newest"]
    assert trimmed_counts == [100, 20, 250, 20]


def test_trim_history_never_drops_system_or_newest_message():
    history = _fake_history(2)
    counts = [5000] + [10] * 4 + [5000]
    trimmed, _ = _trim_history(history, counts, 100)
    assert [message["content"] for message in trimmed] == ["sys", "newest"]


def test_user_message_after_user_message_joins_it():
    conversation = _Conversation(
        websocket=None, asr=None, llm=FakeLLM(), tts=None,
        system_prompt="sys", system_tokens=1, budget=1000,
    )

    async def two_user_messages():
        await conversation._append("user", "I was thinking")
        await conversation._append("user", "about the weekend")

    asyncio.run(two_user_messages())
    assert conversation.history[1:] == [{"role": "user", "content": "I was thinking about the weekend"}]
    assert conversation.token_counts == [1, 6]


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

    monkeypatch.setattr(silero, "load_silero_vad", lambda: None)
    monkeypatch.setattr(silero, "VADIterator", FakeVADIterator)

    frame = np.zeros(512, dtype=np.int16).tobytes()
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        events = [json.loads(websocket.receive_text()) for _ in range(6)]
        assert [event["type"] for event in events] == [
            "speech_start", "transcript", "reply_delta", "reply_delta", "reply_end", "latency",
        ]
        assert events[1]["text"] == "I goes to the store yesterday."
        websocket.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    sessions = client.app.state.db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["kind"] == "live"
    assert "store" in sessions[0]["transcript"]
    assert sessions[0]["metrics"]["word_count"] == 6


class InterruptingVADIterator(FakeVADIterator):
    """One full utterance, then the learner starts to speak again on frame 3."""

    def __call__(self, frame):
        self.calls += 1
        return {1: {"start": 0}, 2: {"end": 1024}, 3: {"start": 2048}}.get(self.calls)


class StallingLLM(FakeLLM):
    """Streams one token and then hangs, like a long reply in progress."""

    async def chat_stream(self, history, max_tokens: int = 256):
        yield "Sounds "
        await asyncio.sleep(60)


def test_learner_speech_cancels_the_reply_in_progress(client, monkeypatch):
    silero = pytest.importorskip("silero_vad")
    monkeypatch.setattr(silero, "load_silero_vad", lambda: None)
    monkeypatch.setattr(silero, "VADIterator", InterruptingVADIterator)
    client.app.state.llm = StallingLLM()

    frame = np.zeros(512, dtype=np.int16).tobytes()
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        opening = [json.loads(websocket.receive_text())["type"] for _ in range(3)]
        assert opening == ["speech_start", "transcript", "reply_delta"]

        # The reply is now stalled for 60 s. Only a cancel lets reply_end arrive.
        websocket.send_bytes(frame)
        after = {json.loads(websocket.receive_text())["type"] for _ in range(2)}
        assert after == {"speech_start", "reply_end"}

        websocket.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    sessions = client.app.state.db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["transcript"] == "I goes to the store yesterday."


def _scripted_vad(script: dict[int, dict], on_call: dict | None = None):
    """A VADIterator stand-in that returns `script[n]` for frame n and runs
    `on_call[n]` first, so a test can release a gated ASR at an exact frame."""
    hooks = on_call or {}

    class ScriptedVADIterator:
        def __init__(self, model, sampling_rate, min_silence_duration_ms):
            self.calls = 0

        def __call__(self, frame):
            self.calls += 1
            if self.calls in hooks:
                hooks[self.calls]()
            return script.get(self.calls)

        def reset_states(self):
            pass

    return ScriptedVADIterator


class GatedASR:
    """Returns the scripted texts in order. The first transcription blocks
    until the gate opens, which holds turn A in its ASR phase."""

    def __init__(self, texts: list[str], gate: threading.Event) -> None:
        self.texts = list(texts)
        self.gate = gate
        self.calls = 0

    def transcribe(self, audio_path) -> Transcription:
        self.calls += 1
        if self.calls == 1:
            assert self.gate.wait(timeout=10)
        text = self.texts[self.calls - 1]
        return Transcription(language="en", duration=1.0, text=text, segments=[])


class RecordingLLM(FakeLLM):
    """Keeps the history that each reply was generated from."""

    def __init__(self) -> None:
        self.reply_histories: list[list[dict[str, str]]] = []

    async def chat_stream(self, history, max_tokens: int = 256):
        self.reply_histories.append([dict(message) for message in history])
        yield "Ok."


def _events_until(websocket, wanted: str) -> list[dict]:
    events = []
    while not events or events[-1]["type"] != wanted:
        events.append(json.loads(websocket.receive_text()))
    return events


def _live_setup(client, monkeypatch, vad_class, asr, llm) -> bytes:
    silero = pytest.importorskip("silero_vad")
    monkeypatch.setattr(silero, "load_silero_vad", lambda: None)
    monkeypatch.setattr(silero, "VADIterator", vad_class)
    client.app.state.asr = asr
    client.app.state.llm = llm
    return np.zeros(512, dtype=np.int16).tobytes()


def _end(websocket) -> None:
    websocket.send_text('{"type": "end"}')
    with pytest.raises(WebSocketDisconnect):
        websocket.receive_text()


def test_second_utterance_during_asr_gets_one_reply_to_both(client, monkeypatch):
    # Utterance B starts AND ends while A is still being transcribed. A must
    # not answer alone: one reply, generated from "A B".
    gate = threading.Event()
    vad = _scripted_vad(
        {1: {"start": 0}, 2: {"end": 1}, 3: {"start": 2}, 4: {"end": 3}},
        on_call={4: gate.set},
    )
    llm = RecordingLLM()
    frame = _live_setup(
        client, monkeypatch, vad, GatedASR(["I was thinking", "about the weekend"], gate), llm
    )

    with client.websocket_connect("/ws/live") as websocket:
        for _ in range(4):
            websocket.send_bytes(frame)
        events = _events_until(websocket, "latency")
        _end(websocket)

    assert [event["type"] for event in events].count("reply_end") == 1
    assert len(llm.reply_histories) == 1
    assert llm.reply_histories[0][-1] == {
        "role": "user", "content": "I was thinking about the weekend",
    }


def test_skipped_reply_is_sent_when_the_next_utterance_is_noise(client, monkeypatch):
    # A's reply is skipped because B has started. B transcribes to nothing, so
    # the reply to A is still owed.
    gate = threading.Event()
    vad = _scripted_vad(
        {1: {"start": 0}, 2: {"end": 1}, 3: {"start": 2}, 4: {"end": 3}},
        on_call={3: gate.set},
    )
    llm = RecordingLLM()
    frame = _live_setup(
        client, monkeypatch, vad, GatedASR(["I goes to the store yesterday.", ""], gate), llm
    )

    with client.websocket_connect("/ws/live") as websocket:
        for _ in range(3):
            websocket.send_bytes(frame)
        _events_until(websocket, "transcript")
        assert llm.reply_histories == []
        websocket.send_bytes(frame)
        events = _events_until(websocket, "latency")
        _end(websocket)

    assert "reply_delta" in [event["type"] for event in events]
    assert len(llm.reply_histories) == 1
    assert llm.reply_histories[0][-1]["content"] == "I goes to the store yesterday."


def test_skipped_reply_is_sent_when_the_next_utterance_is_cut_off(client, monkeypatch):
    gate = threading.Event()
    vad = _scripted_vad({1: {"start": 0}, 2: {"end": 1}, 3: {"start": 2}}, on_call={3: gate.set})
    llm = RecordingLLM()
    frame = _live_setup(
        client, monkeypatch, vad, GatedASR(["I goes to the store yesterday."], gate), llm
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(live_routes, "time", SimpleNamespace(perf_counter=lambda: clock["now"]))

    with client.websocket_connect("/ws/live") as websocket:
        for _ in range(3):
            websocket.send_bytes(frame)
        _events_until(websocket, "transcript")
        clock["now"] = 1000.0  # far past max_utterance_seconds
        websocket.send_bytes(frame)
        events = _events_until(websocket, "latency")
        _end(websocket)

    details = [event.get("detail", "") for event in events if event["type"] == "error"]
    assert any("too long" in detail for detail in details)
    assert len(llm.reply_histories) == 1
    assert llm.reply_histories[0][-1]["content"] == "I goes to the store yesterday."


def test_interrupted_reply_stays_in_history_as_far_as_it_got(client, monkeypatch):
    class StallOnceLLM(RecordingLLM):
        async def chat_stream(self, history, max_tokens: int = 256):
            self.reply_histories.append([dict(message) for message in history])
            if len(self.reply_histories) == 1:
                yield "Sounds "
                await asyncio.sleep(60)
            yield "Ok."

    gate = threading.Event()
    gate.set()
    vad = _scripted_vad({1: {"start": 0}, 2: {"end": 1}, 3: {"start": 2}, 4: {"end": 3}})
    llm = StallOnceLLM()
    frame = _live_setup(client, monkeypatch, vad, GatedASR(["First.", "Second."], gate), llm)

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        _events_until(websocket, "reply_delta")
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        _events_until(websocket, "latency")
        _end(websocket)

    assert len(llm.reply_histories) == 2
    assert [(m["role"], m["content"]) for m in llm.reply_histories[1][1:]] == [
        ("user", "First."), ("assistant", "Sounds "), ("user", "Second."),
    ]


def _wait_for(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_live_slot_stays_taken_until_the_last_turn_finishes(client, monkeypatch):
    # The closing session's last turn still runs on the shared ASR model, so a
    # new session must not start beside it.
    gate = threading.Event()
    vad = _scripted_vad({1: {"start": 0}, 2: {"end": 1}})
    frame = _live_setup(client, monkeypatch, vad, GatedASR(["Last words."], gate), RecordingLLM())
    state = client.app.state

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        _events_until(websocket, "speech_start")
        websocket.send_text('{"type": "end"}')

        released_early = _wait_for(lambda: not state.live_active, 0.5)
        assert not released_early

        gate.set()
        with pytest.raises(WebSocketDisconnect):
            while True:
                websocket.receive_text()

    assert state.live_active is False
    assert state.db.list_sessions()[0]["transcript"] == "Last words."


def test_live_slot_is_free_while_the_session_review_runs(client, monkeypatch):
    # The end-of-session review can take many seconds. A learner who restarts
    # the conversation in that time must not be refused.
    review_gate = threading.Event()

    class SlowReviewLLM(RecordingLLM):
        async def review(self, transcript: str, native_language: str, cefr_level: str) -> Review:
            await asyncio.to_thread(review_gate.wait, 10)
            return Review(overall=OverallFeedback(summary="Reviewed."))

    asr_gate = threading.Event()
    asr_gate.set()
    vad = _scripted_vad({1: {"start": 0}, 2: {"end": 1}})
    frame = _live_setup(client, monkeypatch, vad, GatedASR(["Hello there."], asr_gate), SlowReviewLLM())
    state = client.app.state

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        _events_until(websocket, "latency")
        websocket.send_text('{"type": "end"}')

        assert _wait_for(lambda: not state.live_active, 5)
        assert state.db.list_sessions() == []  # the review has not returned yet

        review_gate.set()
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    assert state.db.list_sessions()[0]["review"]["overall"]["summary"] == "Reviewed."


class CorrectingLLM(FakeLLM):
    """Speaks one correction during the turn and types one in the final review."""

    async def chat_stream(self, history, max_tokens: int = 256):
        yield 'Nice. ↪ "I goes" → "I went" (past tense)'

    async def review(self, transcript: str, native_language: str, cefr_level: str) -> Review:
        return Review(
            corrections=[
                Correction(type="tense", original="I goes", corrected="I went", explanation="Past simple."),
            ],
            overall=OverallFeedback(summary=f"Reviewed: {transcript}"),
        )


def _run_one_turn(client, monkeypatch, llm) -> dict:
    silero = pytest.importorskip("silero_vad")
    monkeypatch.setattr(silero, "load_silero_vad", lambda: None)
    monkeypatch.setattr(silero, "VADIterator", FakeVADIterator)
    client.app.state.llm = llm

    frame = np.zeros(512, dtype=np.int16).tobytes()
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        while json.loads(websocket.receive_text())["type"] != "latency":
            pass
        websocket.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    sessions = client.app.state.db.list_sessions()
    assert len(sessions) == 1
    return sessions[0]


def test_live_session_is_stored_with_a_typed_grammar_review(client, monkeypatch):
    session = _run_one_turn(client, monkeypatch, CorrectingLLM())

    review = session["review"]
    assert review["overall"]["summary"] == "Reviewed: I goes to the store yesterday."
    assert [item["type"] for item in review["corrections"]] == ["tense"]
    assert review["live_corrections"] == [
        {"original": "I goes", "corrected": "I went", "reason": "past tense"},
    ]

    recurring = client.app.state.db.recurring_error_types(min_count=1)
    assert recurring == [{"type": "tense", "count": 1}]


class ReviewDownLLM(CorrectingLLM):
    async def review(self, transcript: str, native_language: str, cefr_level: str) -> Review:
        raise httpx.ConnectError("connection refused")


def test_live_session_is_stored_when_the_final_review_fails(client, monkeypatch):
    session = _run_one_turn(client, monkeypatch, ReviewDownLLM())

    review = session["review"]
    assert "did not run" in review["overall"]["summary"]
    assert review["corrections"] == []
    assert len(review["live_corrections"]) == 1
    assert client.app.state.db.recurring_error_types(min_count=1) == []


def test_live_socket_reports_an_unreachable_llm_at_the_start(client):
    pytest.importorskip("silero_vad")

    class UnreachableLLM(FakeLLM):
        async def context_size(self) -> int:
            raise httpx.ConnectError("connection refused")

    client.app.state.llm = UnreachableLLM()
    with client.websocket_connect("/ws/live") as websocket:
        payload = json.loads(websocket.receive_text())
        assert payload["type"] == "error"
        assert "llama-server" in payload["detail"]


def test_second_concurrent_live_connection_is_rejected(client):
    pytest.importorskip("silero_vad")

    with client.websocket_connect("/ws/live") as first:
        with client.websocket_connect("/ws/live") as second:
            payload = json.loads(second.receive_text())
            assert payload["type"] == "error"
            assert "already active" in payload["detail"]
        first.send_text('{"type": "end"}')
        with pytest.raises(WebSocketDisconnect):
            first.receive_text()


ECHO_LEVEL = 1000


def _recording_vad(script: dict[int, dict]):
    """A VADIterator stand-in that follows `script` and keeps the peak level of
    every frame it was given, so a test can tell which audio reached the VAD."""
    heard: list[float] = []

    class RecordingVADIterator:
        def __init__(self, model, sampling_rate, min_silence_duration_ms):
            self.calls = 0

        def __call__(self, frame):
            self.calls += 1
            heard.append(float(np.abs(frame).max()))
            return script.get(self.calls)

        def reset_states(self):
            pass

    return RecordingVADIterator, heard


def test_microphone_is_ignored_while_a_reply_plays_on_speakers(client, monkeypatch):
    # On speakers the microphone picks up the tutor's voice. The VAD would call
    # frame 4 a speech start and the tutor would interrupt itself.
    vad, heard = _recording_vad({1: {"start": 0}, 2: {"end": 1}, 4: {"start": 2}})
    frame = _live_setup(client, monkeypatch, vad, FakeASR(), FakeLLM())
    echo = np.full(512, ECHO_LEVEL, dtype=np.int16).tobytes()
    tail = np.full(live_routes.PLAYBACK_TAIL_SAMPLES, ECHO_LEVEL, dtype=np.int16).tobytes()

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        _events_until(websocket, "latency")

        websocket.send_text('{"type": "playback_start"}')
        for _ in range(5):
            websocket.send_bytes(echo)
        websocket.send_text('{"type": "playback_end"}')
        websocket.send_bytes(tail)
        websocket.send_bytes(frame)
        websocket.send_bytes(frame)
        assert json.loads(websocket.receive_text())["type"] == "speech_start"
        _end(websocket)

    assert heard == [0.0, 0.0, 0.0, 0.0]


def test_reply_held_back_by_speech_is_sent_when_playback_discards_that_speech(client, monkeypatch):
    # Utterance B starts while A is still in the ASR, so A gets no reply. A
    # playback start then discards B. Nothing else would send the reply to A.
    gate = threading.Event()
    vad = _scripted_vad({1: {"start": 0}, 2: {"end": 1}, 3: {"start": 2}}, on_call={3: gate.set})
    llm = RecordingLLM()
    frame = _live_setup(
        client, monkeypatch, vad, GatedASR(["I goes to the store yesterday."], gate), llm
    )

    with client.websocket_connect("/ws/live") as websocket:
        for _ in range(3):
            websocket.send_bytes(frame)
        _events_until(websocket, "transcript")
        websocket.send_text('{"type": "playback_start"}')
        _events_until(websocket, "latency")
        _end(websocket)

    assert len(llm.reply_histories) == 1
    assert llm.reply_histories[0][-1]["content"] == "I goes to the store yesterday."


class ClosedWebSocket:
    """A socket whose browser side is gone: every send raises."""

    async def send_text(self, text: str) -> None:
        raise WebSocketDisconnect(code=1006)


def test_disconnect_during_a_turn_ends_it_without_an_error_or_a_reply(caplog):
    llm = RecordingLLM()
    conversation = _Conversation(
        websocket=ClosedWebSocket(), asr=FakeASR(), llm=llm, tts=None,
        system_prompt="sys", system_tokens=1, budget=1000,
    )

    async def one_turn():
        await conversation.start_turn(np.zeros(16000, dtype=np.float32), None)

    with caplog.at_level(logging.INFO, logger="backend.routes.live"):
        asyncio.run(one_turn())

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    assert llm.reply_histories == []
    # The session review still needs what the learner said.
    assert conversation.user_turns == ["I goes to the store yesterday."]
