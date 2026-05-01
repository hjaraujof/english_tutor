"""FastAPI app composition. Loads ASR + LLM client + DB once at startup;
serves the static frontend and the /api routes.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.asr import ASR
from backend.config import load_config
from backend.db import Database
from backend.llm import LLMClient
from backend.routes import review as review_routes
from backend.routes import sessions as sessions_routes
from backend.tts import TTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("english_tutor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    logger.info("loading ASR model %s on %s (%s)", config.asr.model_size, config.asr.device, config.asr.compute_type)
    asr = ASR(
        model_size=config.asr.model_size,
        device=config.asr.device,
        compute_type=config.asr.compute_type,
        beam_size=config.asr.beam_size,
    )
    llm = LLMClient(
        base_url=config.llm.base_url,
        model=config.llm.model,
        prompts_dir=config.project_root / "backend" / "prompts",
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
    )
    db = Database(config.server.data_dir / "tutor.db")

    voice_model = config.project_root / "models" / "piper" / f"{config.tts.voice}.onnx"
    tts = TTS(voice_model=voice_model)
    if not tts.is_available():
        logger.info("TTS unavailable (voice model or piper binary missing); live mode will skip audio replies")

    app.state.config = config
    app.state.asr = asr
    app.state.llm = llm
    app.state.db = db
    app.state.tts = tts
    logger.info("english_tutor ready on %s:%d", config.server.host, config.server.port)
    try:
        yield
    finally:
        await llm.aclose()


app = FastAPI(title="English Tutor", version="0.1.0", lifespan=lifespan)
app.include_router(sessions_routes.router)
app.include_router(review_routes.router)

try:
    from backend.routes import live as live_routes
    app.include_router(live_routes.router)
except ImportError as exc:
    logger.info("live conversation route disabled: %s", exc)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def public_config():
    config = app.state.config
    return {
        "native_language": config.user.native_language,
        "cefr_level": config.user.cefr_level,
        "model": config.llm.model,
        "whisper_size": config.asr.model_size,
    }


_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
