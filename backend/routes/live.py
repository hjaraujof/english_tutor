"""Live conversation WebSocket: client streams 16 kHz PCM frames; server runs
VAD → ASR → LLM (streaming) → TTS, sending events back as JSON messages and
audio frames as binary.

Wire protocol (server → client):
  text frames: {"type": "transcript", "text": "..."}
                {"type": "reply_delta", "text": "..."}
                {"type": "reply_end"}
                {"type": "correction", "original": "...", "corrected": "...", "reason": "..."}
                {"type": "latency", "asr_ms": ..., "llm_ms": ..., "tts_ms": ...}
                {"type": "error", "detail": "..."}
  binary frames: WAV blob (mono int16 PCM at the voice's native sample rate) TTS audio

Wire protocol (client → server):
  binary frames: mono int16 PCM mic audio at 16 kHz, any frame size — the server
                 re-chunks internally to the VAD's required window
  text frames: {"type": "end"} to close
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.analysis import compute_metrics

logger = logging.getLogger(__name__)
router = APIRouter()


SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
MAX_HISTORY_TURNS = 12
# While no speech is detected, only this many frames are retained as onset
# context (covers the VAD's speech-pad rewind); prevents unbounded growth and
# keeps idle time out of the transcribed utterance.
PRE_ROLL_FRAMES = 10


# The model does not reliably produce the documented `↪ "a" → "b" (reason)`
# shape: it substitutes typographic quotes, opens with a bare arrow instead of
# ↪, and sometimes omits the parenthetical. Accept those variants rather than
# silently dropping the correction.
_QUOTES = "\"'“”‘’"
_QUOTE = f"[{_QUOTES}]"
_ARROW = "(?:→|->|=>)"
_MARKER = f"(?:↪|{_ARROW})"

_CORRECTION_RE = re.compile(
    rf"{_MARKER}\s*{_QUOTE}([^{_QUOTES}]+){_QUOTE}"
    rf"\s*{_ARROW}\s*{_QUOTE}([^{_QUOTES}]+){_QUOTE}"
    rf"\s*(?:\(([^)]*)\))?"
)

# Any run that opens like a correction marker, whether or not it parses into a
# usable pair. The reply is spoken by the TTS, so a marker the parser could not
# read must still be removed — otherwise the learner hears the arrow and the
# parenthesis read out as words. `↪ none` is the explicit no-correction token,
# which must be stripped too.
_MARKER_SPAN_RE = re.compile(rf"{_MARKER}[ \t]*(?:{_QUOTE}|[Nn]one\b)[^\n]*")


def _split_reply_and_correction(text: str) -> tuple[str, dict | None]:
    correction = None
    match = _CORRECTION_RE.search(text)
    if match:
        original, corrected, reason = match.group(1), match.group(2), match.group(3)
        # A correction whose halves are identical renders as a correction of
        # nothing. The prompt forbids it; this guarantees it.
        if original.strip() != corrected.strip():
            correction = {
                "original": original,
                "corrected": corrected,
                "reason": (reason or "").strip(),
            }

    reply = _MARKER_SPAN_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", reply).strip(), correction


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Bound history to the system message + the newest MAX_HISTORY_TURNS
    complete user/assistant pairs so long conversations never overflow the
    LLM context."""
    if len(history) > 1 + 2 * MAX_HISTORY_TURNS:
        return [history[0]] + history[-2 * MAX_HISTORY_TURNS:]
    return history


@router.websocket("/ws/live")
async def live_socket(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    config = state.config
    asr = state.asr
    llm = state.llm
    db = state.db
    tts = getattr(state, "tts", None)

    if getattr(state, "live_active", False):
        await websocket.send_text(json.dumps({
            "type": "error",
            "detail": "another live conversation is already active",
        }))
        await websocket.close()
        return
    state.live_active = True

    # Collected across the conversation so the session survives the socket:
    # spoken turns, live corrections, and total spoken seconds (for honest WPM).
    user_turns: list[str] = []
    live_corrections: list[dict] = []
    total_speech_seconds = 0.0

    try:
        try:
            from silero_vad import load_silero_vad, VADIterator
        except ImportError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "detail": "silero-vad not installed. Run: uv sync --extra phase2",
            }))
            return

        # torch.jit.load blocks for ~a second: run it off the event loop, once
        # per process. The shared model carries streaming state, so the
        # live_active guard above keeps live connections exclusive.
        vad_model = getattr(state, "vad_model", None)
        if vad_model is None:
            vad_model = await asyncio.to_thread(load_silero_vad)
            state.vad_model = vad_model
        vad = VADIterator(
            vad_model,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=config.live.silence_end_ms,
        )

        prompt_path = Path(config.project_root) / "backend" / "prompts" / "conversation_partner.md"
        system_prompt = (await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")).format(
            native_language=config.user.native_language,
            cefr_level=config.user.cefr_level,
        )
        history: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

        audio_buffer: list[np.ndarray] = []
        pcm_residual = np.empty(0, dtype=np.float32)
        in_speech = False
        speech_start_time: float | None = None

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message and message["text"]:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "end":
                    break
                continue

            data = message.get("bytes")
            if not data or len(data) % 2 != 0:
                continue

            incoming = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if incoming.size == 0:
                continue
            pcm_residual = np.concatenate([pcm_residual, incoming])

            while pcm_residual.size >= FRAME_SAMPLES:
                frame = pcm_residual[:FRAME_SAMPLES]
                pcm_residual = pcm_residual[FRAME_SAMPLES:]

                audio_buffer.append(frame)
                if not in_speech and len(audio_buffer) > PRE_ROLL_FRAMES:
                    del audio_buffer[0]

                vad_event = vad(frame)
                if vad_event:
                    if "start" in vad_event and not in_speech:
                        in_speech = True
                        speech_start_time = time.perf_counter()
                    if "end" in vad_event and in_speech:
                        utterance = np.concatenate(audio_buffer)
                        audio_buffer = []
                        in_speech = False
                        if speech_start_time is None:
                            continue
                        asr_started = time.perf_counter()
                        transcription = await _transcribe_pcm(asr, utterance)
                        asr_ms = int((time.perf_counter() - asr_started) * 1000)
                        if not transcription.strip():
                            continue

                        await websocket.send_text(json.dumps({"type": "transcript", "text": transcription}))
                        history.append({"role": "user", "content": transcription})
                        user_turns.append(transcription)
                        total_speech_seconds += utterance.size / SAMPLE_RATE

                        llm_started = time.perf_counter()
                        accumulated = ""
                        async for delta in llm.chat_stream(history, max_tokens=256):
                            accumulated += delta
                            await websocket.send_text(json.dumps({"type": "reply_delta", "text": delta}))
                        llm_ms = int((time.perf_counter() - llm_started) * 1000)

                        reply, correction = _split_reply_and_correction(accumulated)
                        history.append({"role": "assistant", "content": accumulated})
                        history = _trim_history(history)
                        await websocket.send_text(json.dumps({"type": "reply_end"}))
                        if correction:
                            await websocket.send_text(json.dumps({"type": "correction", **correction}))
                            live_corrections.append(correction)

                        tts_ms = 0
                        if tts is not None and reply and tts.is_available():
                            tts_started = time.perf_counter()
                            wav = await tts.synthesize(reply)
                            tts_ms = int((time.perf_counter() - tts_started) * 1000)
                            if wav:
                                await websocket.send_bytes(wav)

                        await websocket.send_text(json.dumps({
                            "type": "latency",
                            "asr_ms": asr_ms,
                            "llm_ms": llm_ms,
                            "tts_ms": tts_ms,
                        }))

                if in_speech and speech_start_time is not None:
                    if time.perf_counter() - speech_start_time > config.live.max_utterance_seconds:
                        audio_buffer = []
                        in_speech = False
                        speech_start_time = None
                        # VADIterator still holds triggered state after a forced
                        # cut; without a reset its next 'end' event would be
                        # dropped and the overflow would bleed into the next turn.
                        vad.reset_states()
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": "Utterance too long; cut off.",
                        }))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures
        logger.exception("live socket error")
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": str(exc)}))
        except Exception:
            pass
    finally:
        state.live_active = False
        if user_turns:
            transcript_text = " ".join(user_turns)
            review_dump = {
                "corrections": [
                    {
                        "type": "live",
                        "original": item["original"],
                        "corrected": item["corrected"],
                        "explanation": item["reason"],
                        "severity": "medium",
                    }
                    for item in live_corrections
                ],
                "fluency_notes": [],
                "vocabulary_suggestions": [],
                "overall": {
                    "summary": f"Live conversation: {len(user_turns)} spoken turn(s).",
                    "strengths": [],
                    "next_focus": [],
                },
            }
            metrics = compute_metrics(text=transcript_text, duration_seconds=total_speech_seconds)
            try:
                await asyncio.to_thread(
                    lambda: db.insert_session(
                        kind="live",
                        transcript=transcript_text,
                        review=review_dump,
                        metrics=metrics.to_dict(),
                    )
                )
            except Exception:
                logger.exception("failed to persist live session")
        try:
            await websocket.close()
        except Exception:
            pass


async def _transcribe_pcm(asr, samples: np.ndarray) -> str:
    """Write a temp wav and let faster-whisper read it. Trades a tiny amount of
    latency for code simplicity vs. plumbing in-memory ndarrays through CTranslate2.
    """
    import io
    import wave

    buffer = io.BytesIO()
    int16_samples = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(int16_samples.tobytes())
    buffer.seek(0)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as temp_file:
        temp_file.write(buffer.getvalue())
        temp_file.flush()
        transcription = await asyncio.to_thread(asr.transcribe, Path(temp_file.name))
    return transcription.text
