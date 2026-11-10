"""TTS wrapper tests. A stub voice locks the wire contract (a WAV blob at the
voice's native sample rate), the load-once behaviour and the cancel path. One
test runs the real Piper voice when the package and the model are present."""
from __future__ import annotations

import asyncio
import io
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import tts as tts_module
from backend.config import load_config
from backend.tts import TTS

CHUNK_BYTES = b"\x00" * 100


class StubVoice:
    """Yields one chunk per sentence, as Piper does."""

    def __init__(self, sentences: int = 2) -> None:
        self.sentences = sentences
        self.yielded = 0
        self.first_chunk_out = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()

    def synthesize(self, text: str):
        for _ in range(self.sentences):
            self.yielded += 1
            self.first_chunk_out.set()
            yield SimpleNamespace(sample_rate=22050, audio_int16_bytes=CHUNK_BYTES)
            assert self.proceed.wait(timeout=10)


@pytest.fixture
def voice_files(tmp_path: Path) -> Path:
    voice = tmp_path / "en_US-amy-medium.onnx"
    voice.write_bytes(b"onnx-stub")
    Path(f"{voice}.json").write_text("{}", encoding="utf-8")
    return voice


@pytest.fixture
def stub_loader(monkeypatch):
    """Replaces the Piper loader. `loads` records each call; `voice` is what it returns."""
    loader = SimpleNamespace(loads=[], voice=StubVoice())

    def load(voice_model: Path):
        loader.loads.append(voice_model)
        return loader.voice

    monkeypatch.setattr(tts_module, "_load_piper_voice", load)
    return loader


@pytest.mark.asyncio
async def test_synthesize_returns_wav_at_voice_native_rate(voice_files, stub_loader):
    blob = await TTS(voice_model=voice_files).synthesize("Hello there. How are you?")
    assert blob[:4] == b"RIFF"
    with wave.open(io.BytesIO(blob)) as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 22050
        assert reader.readframes(reader.getnframes()) == CHUNK_BYTES * 2


@pytest.mark.asyncio
async def test_voice_is_loaded_once_for_many_replies(voice_files, stub_loader):
    tts = TTS(voice_model=voice_files)
    tts.load_voice()
    await tts.synthesize("First reply.")
    await tts.synthesize("Second reply.")
    assert stub_loader.loads == [voice_files]


def test_load_voice_runs_one_warm_up_synthesis(voice_files, stub_loader):
    stub_loader.voice = StubVoice(sentences=1)
    TTS(voice_model=voice_files).load_voice()
    assert stub_loader.voice.yielded == 1


@pytest.mark.asyncio
async def test_empty_text_synthesizes_nothing_and_loads_nothing(voice_files, stub_loader):
    assert await TTS(voice_model=voice_files).synthesize("   ") == b""
    assert stub_loader.loads == []


@pytest.mark.asyncio
async def test_cancelled_reply_stops_at_the_next_sentence(voice_files, stub_loader):
    cancelled_voice = StubVoice(sentences=3)
    cancelled_voice.proceed.clear()
    stub_loader.voice = cancelled_voice
    tts = TTS(voice_model=voice_files)

    task = asyncio.create_task(tts.synthesize("One. Two. Three."))
    assert await asyncio.to_thread(cancelled_voice.first_chunk_out.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled_voice.proceed.set()

    # The next reply waits for the cancelled worker to leave the voice, so by
    # the time it returns the cancelled synthesis has stopped.
    assert (await tts.synthesize("Next."))[:4] == b"RIFF"
    assert cancelled_voice.yielded == 2 + 3  # sentence three of the first reply never ran


@pytest.mark.asyncio
async def test_failed_synthesis_returns_no_audio(voice_files, monkeypatch, caplog):
    def broken_loader(voice_model: Path):
        raise RuntimeError("model file is corrupt")

    monkeypatch.setattr(tts_module, "_load_piper_voice", broken_loader)
    assert await TTS(voice_model=voice_files).synthesize("Hello.") == b""
    assert "model file is corrupt" in caplog.text


def test_unavailable_without_voice_config(tmp_path: Path):
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"onnx-stub")
    assert not TTS(voice_model=voice).is_available()


@pytest.mark.asyncio
async def test_real_voice_speaks_a_sentence():
    pytest.importorskip("piper")
    config = load_config()
    voice_model = config.project_root / "models" / "piper" / f"{config.tts.voice}.onnx"
    if not voice_model.exists():
        pytest.skip(f"voice model not installed at {voice_model}")

    tts = TTS(voice_model=voice_model)
    assert tts.is_available()
    blob = await tts.synthesize("Thanks for sharing. What did you do after you got home?")

    with wave.open(io.BytesIO(blob)) as reader:
        assert reader.getframerate() == 22050
        seconds = reader.getnframes() / reader.getframerate()
    assert 1.5 < seconds < 10
