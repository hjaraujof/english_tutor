"""Shared fixtures: a TestClient wired to the real routers with stubbed ASR/LLM
and a real SQLite DB on tmp_path. No network, no GPU, no model loads."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.asr import Segment, Transcription, Word
from backend.config import load_config
from backend.db import Database
from backend.llm import OverallFeedback, Review
from backend.routes import live as live_routes
from backend.routes import review as review_routes
from backend.routes import sessions as sessions_routes


class FakeASR:
    """Returns a canned transcription without loading faster-whisper."""

    def __init__(self, text: str = "I goes to the store yesterday.") -> None:
        self.text = text

    def transcribe(self, audio_path: Path) -> Transcription:
        word = Word(text="I", start=0.0, end=0.4, probability=0.99)
        segment = Segment(text=self.text, start=0.0, end=2.0, words=[word])
        return Transcription(language="en", duration=2.0, text=self.text, segments=[segment])


class RaisingASR:
    """Simulates an ASR backend that blows up mid-transcription."""

    def transcribe(self, audio_path: Path) -> Transcription:
        raise RuntimeError("model exploded")


class FakeLLM:
    """Async stub for LLMClient.review — no httpx, no llama-server."""

    async def review(self, transcript: str, native_language: str, cefr_level: str) -> Review:
        return Review(
            overall=OverallFeedback(summary="Looks good.", strengths=["clarity"], next_focus=["tense"]),
        )


def _build_config(tmp_path: Path):
    config = load_config()
    data_dir = tmp_path / "data"
    (data_dir / "audio").mkdir(parents=True, exist_ok=True)
    server = dataclasses.replace(config.server, data_dir=data_dir)
    return dataclasses.replace(config, server=server)


def _build_app(tmp_path: Path, asr) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_routes.router)
    app.include_router(review_routes.router)
    app.include_router(live_routes.router)
    app.state.config = _build_config(tmp_path)
    app.state.db = Database(tmp_path / "tutor.db")
    app.state.asr = asr
    app.state.llm = FakeLLM()
    app.state.tts = None
    return app


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return _build_app(tmp_path, FakeASR())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def raising_client(tmp_path: Path) -> TestClient:
    app = _build_app(tmp_path, RaisingASR())
    with TestClient(app) as test_client:
        yield test_client
