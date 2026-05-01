"""faster-whisper ASR wrapper. Loaded once at app startup; transcribe() reuses it.

If CUDA initialization fails (e.g. driver/runtime mismatch on the bundled CT2
wheels), automatically falls back to CPU with a logged warning so the rest of
the app can still boot.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


@dataclass
class Word:
    text: str
    start: float
    end: float
    probability: float


@dataclass
class Segment:
    text: str
    start: float
    end: float
    words: list[Word]


@dataclass
class Transcription:
    language: str
    duration: float
    text: str
    segments: list[Segment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": self.duration,
            "text": self.text,
            "segments": [
                {**asdict(segment)} for segment in self.segments
            ],
        }


class ASR:
    def __init__(self, model_size: str, device: str, compute_type: str, beam_size: int) -> None:
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            self.device = device
            self.compute_type = compute_type
        except (RuntimeError, ValueError) as exc:
            if device == "cuda":
                logger.warning(
                    "CUDA ASR init failed (%s); falling back to CPU with int8. "
                    "Fix by aligning the ctranslate2 wheel with your NVIDIA driver.",
                    exc,
                )
                self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
                self.device = "cpu"
                self.compute_type = "int8"
            else:
                raise
        self.beam_size = beam_size

    def transcribe(self, audio_path: Path) -> Transcription:
        segments_iter, info = self.model.transcribe(
            str(audio_path),
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        segments: list[Segment] = []
        full_text_parts: list[str] = []
        for segment in segments_iter:
            words = [
                Word(text=word.word, start=word.start, end=word.end, probability=word.probability)
                for word in (segment.words or [])
            ]
            segments.append(
                Segment(text=segment.text, start=segment.start, end=segment.end, words=words)
            )
            full_text_parts.append(segment.text)
        return Transcription(
            language=info.language,
            duration=info.duration,
            text="".join(full_text_parts).strip(),
            segments=segments,
        )
