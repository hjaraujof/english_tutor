"""TTS wrapper tests with a stub piper binary — no real piper, no voice model.
Locks the wire contract: synthesize() returns a WAV blob whose header carries
the voice's native sample rate read from <voice>.onnx.json."""
from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest

from backend.tts import TTS

PCM_BYTES = b"\x00" * 200


@pytest.fixture
def stub_voice(tmp_path: Path) -> TTS:
    voice = tmp_path / "en_US-amy-medium.onnx"
    voice.write_bytes(b"onnx-stub")
    (tmp_path / "en_US-amy-medium.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8"
    )
    piper = tmp_path / "piper"
    piper.write_text("#!/bin/sh\ncat > /dev/null\nhead -c 200 /dev/zero\n", encoding="utf-8")
    piper.chmod(0o755)
    return TTS(voice_model=voice, piper_binary=str(piper))


@pytest.mark.asyncio
async def test_synthesize_returns_wav_at_voice_native_rate(stub_voice: TTS):
    blob = await stub_voice.synthesize("Hello there.")
    assert blob[:4] == b"RIFF"
    with wave.open(io.BytesIO(blob)) as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 22050
        assert reader.readframes(reader.getnframes()) == PCM_BYTES


@pytest.mark.asyncio
async def test_empty_text_synthesizes_nothing(stub_voice: TTS):
    assert await stub_voice.synthesize("   ") == b""


def test_unavailable_without_voice_config(tmp_path: Path):
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"onnx-stub")
    tts = TTS(voice_model=voice, piper_binary="/bin/sh")
    assert not tts.is_available()
