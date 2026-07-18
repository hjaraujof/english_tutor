"""Piper TTS wrapper. Spawns piper as a subprocess and synthesizes WAV audio.

Piper is launched via the `piper` binary (install with nix or pip). The voice
model file (.onnx + .onnx.json) lives under models/piper/<voice>.onnx.

This module is intentionally subprocess-based — Piper is a CPU-only C++ binary,
so we avoid pulling in heavy Python wrappers and just feed it text on stdin.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import wave
from pathlib import Path

logger = logging.getLogger(__name__)


class TTS:
    def __init__(self, voice_model: Path, piper_binary: str | None = None) -> None:
        self.voice_model = voice_model
        # Piper voices ship as <voice>.onnx + <voice>.onnx.json.
        self.voice_config = Path(f"{voice_model}.json")
        self.piper_binary = piper_binary or shutil.which("piper") or "piper"
        self._sample_rate: int | None = None

    def is_available(self) -> bool:
        return (
            self.voice_model.exists()
            and self.voice_config.exists()
            and shutil.which(self.piper_binary) is not None
        )

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to a WAV blob (mono int16 PCM at the voice's native rate).

        Piper's --output_raw emits headerless PCM at the sample rate declared in
        the voice's .onnx.json (22050 Hz for medium-quality voices, not 16 kHz);
        the WAV header written here carries that true rate so any standard
        decoder plays it correctly.
        """
        if not text.strip():
            return b""
        if self._sample_rate is None:
            voice = json.loads(
                await asyncio.to_thread(self.voice_config.read_text, encoding="utf-8")
            )
            self._sample_rate = int(voice["audio"]["sample_rate"])
        process = await asyncio.create_subprocess_exec(
            self.piper_binary,
            "--model",
            str(self.voice_model),
            "--output_raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await process.communicate(input=text.encode("utf-8"))
        except BaseException:  # includes CancelledError: never leave piper orphaned
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            logger.warning("piper exited %d: %s", process.returncode, stderr_bytes.decode(errors="ignore"))
            return b""
        if not stdout_bytes:
            return b""
        blob = io.BytesIO()
        with wave.open(blob, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self._sample_rate)
            writer.writeframes(stdout_bytes)
        return blob.getvalue()
