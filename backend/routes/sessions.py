"""Audio review path: POST audio → ASR → LLM review → metrics → DB → JSON response.

Stores the original audio under data/audio/<timestamp>_<id>.<ext> and saves
review + metrics for trend display in the History tab.
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from backend.analysis import compute_metrics
from backend.asr import ASR


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _get_state(request: Request):
    return request.app.state


@router.post("")
async def create_session(
    audio: UploadFile = File(...),
    state=Depends(_get_state),
):
    asr: ASR = state.asr
    db = state.db
    config = state.config
    llm = state.llm

    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    audio_dir: Path = config.server.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{timestamp}_{file_id}{suffix}"

    with audio_path.open("wb") as handle:
        shutil.copyfileobj(audio.file, handle)
    await audio.close()

    try:
        transcription = asr.transcribe(audio_path)
    except Exception as exc:  # noqa: BLE001 - surface ASR failure to client
        raise HTTPException(status_code=500, detail=f"ASR failed: {exc}") from exc

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

    session_id = db.insert_session(
        kind="audio",
        transcript=transcription.text,
        review=review_dump,
        metrics=metrics.to_dict(),
        audio_path=audio_path,
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
    return {"sessions": state.db.list_sessions(limit=limit)}


@router.get("/recurring-errors")
async def recurring_errors(state=Depends(_get_state), days: int = 30, min_count: int = 3):
    return {"recurring": state.db.recurring_error_types(lookback_days=days, min_count=min_count)}


@router.get("/trend/{metric_key}")
async def metric_trend(metric_key: str, state=Depends(_get_state), limit: int = 50):
    return {"key": metric_key, "points": state.db.metric_trend(metric_key, limit=limit)}
