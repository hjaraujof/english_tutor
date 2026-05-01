"""Piper TTS wrapper. Spawns piper as a subprocess and synthesizes WAV to stdout.

Piper is launched via the `piper` binary (install with nix or pip). The voice
model file (.onnx + .onnx.json) lives under models/piper/<voice>.onnx.

This module is intentionally subprocess-based — Piper is a CPU-only C++ binary,
so we avoid pulling in heavy Python wrappers and just feed it text on stdin.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class TTS:
    def __init__(self, voice_model: Path, piper_binary: str | None = None) -> None:
        self.voice_model = voice_model
        self.piper_binary = piper_binary or shutil.which("piper") or "piper"

    def is_available(self) -> bool:
        return self.voice_model.exists() and shutil.which(self.piper_binary) is not None

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to a single WAV blob (16 kHz mono PCM)."""
        if not text.strip():
            return b""
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
        except Exception:
            process.kill()
            raise
        if process.returncode != 0:
            logger.warning("piper exited %d: %s", process.returncode, stderr_bytes.decode(errors="ignore"))
            return b""
        return stdout_bytes
