"""ASR model resolution: where the Whisper files live, and which failures
reach the CUDA-to-CPU fallback. faster-whisper's two entry points are stubbed,
so no model loads and nothing touches the network."""
from __future__ import annotations

from pathlib import Path

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from backend import asr as asr_module
from backend.asr import ASR, ModelUnavailableError


class StubWhisperModel:
    created: list[tuple[str, str, str]] = []
    failing_devices: set[str] = set()

    def __init__(self, model_path: str, device: str, compute_type: str) -> None:
        if device in self.failing_devices:
            raise RuntimeError(f"Library libcublas.so.12 is not found ({device})")
        self.created.append((model_path, device, compute_type))


@pytest.fixture
def stub_whisper(monkeypatch):
    StubWhisperModel.created = []
    StubWhisperModel.failing_devices = set()
    monkeypatch.setattr(asr_module, "WhisperModel", StubWhisperModel)
    return StubWhisperModel


def _model_on_disk(model_dir: Path) -> str:
    snapshot = model_dir / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")
    return str(snapshot)


def test_model_on_disk_loads_without_a_network_call(tmp_path, monkeypatch, stub_whisper):
    snapshot = _model_on_disk(tmp_path)
    calls = []

    def fake_download(model_size, cache_dir, local_files_only=False):
        calls.append((model_size, cache_dir, local_files_only))
        return snapshot

    monkeypatch.setattr(asr_module, "download_model", fake_download)

    asr = ASR("distil-small.en", "cuda", "int8", beam_size=1, model_dir=tmp_path)

    assert calls == [("distil-small.en", str(tmp_path), True)]
    assert stub_whisper.created == [(snapshot, "cuda", "int8")]
    assert asr.device == "cuda"


def test_missing_model_is_downloaded_into_the_project_store(tmp_path, monkeypatch, stub_whisper):
    calls = []

    def fake_download(model_size, cache_dir, local_files_only=False):
        calls.append((cache_dir, local_files_only))
        if local_files_only:
            raise LocalEntryNotFoundError("not cached")
        return _model_on_disk(tmp_path)

    monkeypatch.setattr(asr_module, "download_model", fake_download)

    ASR("distil-small.en", "cuda", "int8", beam_size=1, model_dir=tmp_path)

    assert calls == [(str(tmp_path), True), (str(tmp_path), False)]


def test_failed_download_is_not_reported_as_a_cuda_failure(tmp_path, monkeypatch, stub_whisper, caplog):
    def fake_download(model_size, cache_dir, local_files_only=False):
        if local_files_only:
            raise LocalEntryNotFoundError("not cached")
        # The exact class the Hugging Face transfer raised on a dropped connection.
        raise RuntimeError("File reconstruction error: CAS Client Error")

    monkeypatch.setattr(asr_module, "download_model", fake_download)

    with pytest.raises(ModelUnavailableError, match="could not download whisper model distil-small.en"):
        ASR("distil-small.en", "cuda", "int8", beam_size=1, model_dir=tmp_path)

    assert stub_whisper.created == []
    assert "CUDA" not in caplog.text


def test_cuda_failure_falls_back_to_cpu_int8(tmp_path, monkeypatch, stub_whisper):
    snapshot = _model_on_disk(tmp_path)
    monkeypatch.setattr(asr_module, "download_model", lambda *args, **kwargs: snapshot)
    stub_whisper.failing_devices = {"cuda"}

    asr = ASR("distil-small.en", "cuda", "int8_float16", beam_size=1, model_dir=tmp_path)

    assert stub_whisper.created == [(snapshot, "cpu", "int8")]
    assert (asr.device, asr.compute_type) == ("cpu", "int8")
