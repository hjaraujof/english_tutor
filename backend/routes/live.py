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
  binary frames: raw 16 kHz mono int16 PCM TTS audio

Wire protocol (client → server):
  binary frames: raw 16 kHz mono int16 PCM mic audio
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

logger = logging.getLogger(__name__)
router = APIRouter()


SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_END_MS = 600
MAX_UTTERANCE_SECONDS = 30.0
MAX_HISTORY_TURNS = 12


_CORRECTION_RE = re.compile(r"↪\s*\"([^\"]+)\"\s*→\s*\"([^\"]+)\"\s*\(([^)]+)\)")


def _split_reply_and_correction(text: str) -> tuple[str, dict | None]:
    match = _CORRECTION_RE.search(text)
    if not match:
        return text.strip(), None
    reply = text[: match.start()].strip()
    return reply, {
        "original": match.group(1),
        "corrected": match.group(2),
        "reason": match.group(3).strip(),
    }


@router.websocket("/ws/live")
async def live_socket(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    config = state.config
    asr = state.asr
    llm = state.llm
    tts = getattr(state, "tts", None)

    try:
        from silero_vad import load_silero_vad, VADIterator
    except ImportError:
        await websocket.send_text(json.dumps({
            "type": "error",
            "detail": "silero-vad not installed. Run: uv sync --extra phase2",
        }))
        await websocket.close()
        return

    vad_model = load_silero_vad()
    vad = VADIterator(vad_model, sampling_rate=SAMPLE_RATE, min_silence_duration_ms=SILENCE_END_MS)

    history: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (Path(config.project_root) / "backend" / "prompts" / "conversation_partner.md")
                .read_text(encoding="utf-8")
                .format(
                    native_language=config.user.native_language,
                    cefr_level=config.user.cefr_level,
                ),
        }
    ]

    audio_buffer: list[np.ndarray] = []
    in_speech = False
    speech_start_time: float | None = None

    try:
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
            if not data:
                continue

            frame = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if frame.size == 0:
                continue
            audio_buffer.append(frame)

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

                    llm_started = time.perf_counter()
                    accumulated = ""
                    async for delta in llm.chat_stream(history, max_tokens=256):
                        accumulated += delta
                        await websocket.send_text(json.dumps({"type": "reply_delta", "text": delta}))
                    llm_ms = int((time.perf_counter() - llm_started) * 1000)

                    reply, correction = _split_reply_and_correction(accumulated)
                    history.append({"role": "assistant", "content": accumulated})
                    if len(history) > 1 + 2 * MAX_HISTORY_TURNS:
                        history = [history[0]] + history[-2 * MAX_HISTORY_TURNS:]
                    await websocket.send_text(json.dumps({"type": "reply_end"}))
                    if correction:
                        await websocket.send_text(json.dumps({"type": "correction", **correction}))

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
                if time.perf_counter() - speech_start_time > MAX_UTTERANCE_SECONDS:
                    audio_buffer = []
                    in_speech = False
                    speech_start_time = None
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
