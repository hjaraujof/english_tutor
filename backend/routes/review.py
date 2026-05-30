"""Text-only grammar review path: bypasses ASR, runs LLM directly."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.analysis import compute_metrics


router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


def _get_state(request: Request):
    return request.app.state


@router.post("")
async def review_text(payload: ReviewRequest, state=Depends(_get_state)):
    config = state.config
    llm = state.llm
    db = state.db

    review = await llm.review(
        transcript=payload.text,
        native_language=config.user.native_language,
        cefr_level=config.user.cefr_level,
    )
    review_dump = review.model_dump(by_alias=True)
    metrics = compute_metrics(text=payload.text, duration_seconds=0.0, segments=None)

    session_id = await asyncio.to_thread(
        lambda: db.insert_session(
            kind="text",
            transcript=payload.text,
            review=review_dump,
            metrics=metrics.to_dict(),
        )
    )

    return {
        "id": session_id,
        "review": review_dump,
        "metrics": metrics.to_dict(),
    }
