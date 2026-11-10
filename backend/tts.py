"""Piper TTS wrapper. Loads the voice once and synthesizes WAV audio in-process.

The piper-tts package comes with the phase2 extra. The voice model file
(.onnx + .onnx.json) lives under models/piper/<voice>.onnx.

Loading the voice costs several seconds of CPU, far more than synthesizing a
short reply, so the voice stays in memory for the life of the process.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_piper_voice(voice_model: Path):
    from piper import PiperVoice  # phase2 extra: absent from a default install

    return PiperVoice.load(voice_model)


class TTS:
    def __init__(self, voice_model: Path) -> None:
        self.voice_model = voice_model
        # Piper voices ship as <voice>.onnx + <voice>.onnx.json.
        self.voice_config = Path(f"{voice_model}.json")
        self._voice = None
        # A cancelled reply's worker thread can still be inside the voice when
        # the next reply starts, so one synthesis runs at a time.
        self._voice_lock = threading.Lock()

    def is_available(self) -> bool:
        return (
            self.voice_model.exists()
            and self.voice_config.exists()
            and importlib.util.find_spec("piper") is not None
        )

    def load_voice(self) -> None:
        """Load the voice and run it once. The first synthesis after a load is
        several seconds slower than the rest, and this keeps that cost out of
        the learner's first reply."""
        with self._voice_lock:
            self._ensure_voice()
            for _ in self._voice.synthesize("Ready."):
                pass

    def _ensure_voice(self) -> None:
        if self._voice is None:
            self._voice = _load_piper_voice(self.voice_model)

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to a WAV blob (mono int16 PCM at the voice's native rate).

        The rate comes from the voice itself (22050 Hz for medium-quality
        voices, not 16 kHz), so any standard decoder plays the blob correctly.
        A synthesis that fails returns no audio: the reply text has already
        reached the learner.
        """
        if not text.strip():
            return b""
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(self._synthesize_blocking, text, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception:  # noqa: BLE001 — a missing voice must not fail the turn
            logger.exception("piper synthesis failed")
            return b""

    def _synthesize_blocking(self, text: str, cancelled: threading.Event) -> bytes:
        pcm = bytearray()
        sample_rate = 0
        with self._voice_lock:
            self._ensure_voice()
            # Piper yields one chunk per sentence, which is where a cancelled
            # reply stops.
            for chunk in self._voice.synthesize(text):
                if cancelled.is_set():
                    return b""
                sample_rate = chunk.sample_rate
                pcm += chunk.audio_int16_bytes
        if not pcm:
            return b""
        blob = io.BytesIO()
        with wave.open(blob, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(bytes(pcm))
        return blob.getvalue()
