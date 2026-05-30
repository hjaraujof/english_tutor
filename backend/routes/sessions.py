"""Audio review path: POST audio → ASR → LLM review → metrics → DB → JSON response.

Stores the original audio under data/audio/<timestamp>_<id>.<ext> and saves
review + metrics for trend display in the History tab.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from backend.analysis import compute_metrics
from backend.asr import ASR


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

ALLOWED_AUDIO_SUFFIXES = {".webm", ".wav", ".ogg", ".mp3", ".m4a"}


def _get_state(request: Request):
    return request.app.state


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("could not remove temp audio %s: %s", path, exc)


@router.post("")
async def create_session(
    audio: UploadFile = File(...),
    state=Depends(_get_state),
):
    asr: ASR = state.asr
    db = state.db
    config = state.config
    llm = state.llm

    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES:
        suffix = ".webm"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    audio_dir: Path = config.server.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{timestamp}_{file_id}{suffix}"

    temp_handle = tempfile.NamedTemporaryFile(dir=audio_dir, suffix=suffix + ".part", delete=False)
    temp_path = Path(temp_handle.name)
    try:
        try:
            with temp_handle:
                shutil.copyfileobj(audio.file, temp_handle)
        except OSError as exc:
            raise HTTPException(status_code=400, detail="could not store uploaded audio") from exc
        finally:
            await audio.close()

        transcription = await asyncio.to_thread(asr.transcribe, temp_path)
    except HTTPException:
        _unlink_quietly(temp_path)
        raise
    except Exception as exc:  # noqa: BLE001 - surface ASR failure to client
        _unlink_quietly(temp_path)
        raise HTTPException(status_code=500, detail=f"ASR failed: {exc}") from exc

    os.replace(temp_path, audio_path)

    metrics = compute_metrics(
        text=transcription.text,
        duration_seconds=transcription.duration,
        segments=[
            {"text": seg.text, "start": seg.start, "end": seg.end}
            for seg in transcription.segments
        ],
    )

    if not transcription.text.strip():
        review_dump = {
            "corrections": [],
            "fluency_notes": [],
            "vocabulary_suggestions": [],
            "overall": {"summary": "No speech detected. Try recording a longer sample.", "strengths": [], "next_focus": []},
        }
    else:
        review = await llm.review(
            transcript=transcription.text,
            native_language=config.user.native_language,
            cefr_level=config.user.cefr_level,
        )
        review_dump = review.model_dump(by_alias=True)

    session_id = await asyncio.to_thread(
        lambda: db.insert_session(
            kind="audio",
            transcript=transcription.text,
            review=review_dump,
            metrics=metrics.to_dict(),
            audio_path=audio_path,
        )
    )

    return {
        "id": session_id,
        "transcript": transcription.text,
        "language": transcription.language,
        "segments": [
            {
                "text": seg.text,
                "start": seg.start,
                "end": seg.end,
                "words": [
                    {"text": word.text, "start": word.start, "end": word.end, "probability": word.probability}
                    for word in seg.words
                ],
            }
            for seg in transcription.segments
        ],
        "metrics": metrics.to_dict(),
        "review": review_dump,
    }


@router.get("")
async def list_sessions(state=Depends(_get_state), limit: int = 50):
    sessions = await asyncio.to_thread(state.db.list_sessions, limit=limit)
    return {"sessions": sessions}


@router.get("/recurring-errors")
async def recurring_errors(state=Depends(_get_state), days: int = 30, min_count: int = 3):
    recurring = await asyncio.to_thread(
        state.db.recurring_error_types, lookback_days=days, min_count=min_count
    )
    return {"recurring": recurring}


@router.get("/trend/{metric_key}")
async def metric_trend(metric_key: str, state=Depends(_get_state)):
    points = await asyncio.to_thread(state.db.metric_trend, metric_key)
    return {"key": metric_key, "points": points}
