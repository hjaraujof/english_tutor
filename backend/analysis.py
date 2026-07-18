"""Pure-Python fluency metrics computed from transcript + word timestamps.
No LLM, no network — used for objective per-session numbers and trends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


FILLER_TOKENS = {
    "um", "uh", "uhm", "erm", "er", "ah", "like", "y'know", "you know", "i mean",
    "kinda", "sorta", "basically", "literally", "actually",
}

WORD_RE = re.compile(r"[A-Za-z']+")


@dataclass
class FluencyMetrics:
    word_count: int
    unique_words: int
    duration_seconds: float
    words_per_minute: float
    type_token_ratio: float
    filler_count: int
    filler_ratio: float
    mean_segment_words: float
    longest_pause_seconds: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "word_count": self.word_count,
            "unique_words": self.unique_words,
            "duration_seconds": round(self.duration_seconds, 2),
            "words_per_minute": round(self.words_per_minute, 1),
            "type_token_ratio": round(self.type_token_ratio, 3),
            "filler_count": self.filler_count,
            "filler_ratio": round(self.filler_ratio, 3),
            "mean_segment_words": round(self.mean_segment_words, 2),
            "longest_pause_seconds": round(self.longest_pause_seconds, 2),
        }


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD_RE.finditer(text)]


def _count_fillers(tokens: list[str]) -> int:
    """Count filler words/phrases on token boundaries.

    Multi-word phrases are matched against consecutive token windows, never as
    raw substrings — "I meant" must not count as "i mean", nor "you knowledge"
    as "you know".
    """
    single = sum(1 for token in tokens if token in FILLER_TOKENS)
    multi = 0
    for phrase in FILLER_TOKENS:
        if " " not in phrase:
            continue
        phrase_tokens = phrase.split()
        span = len(phrase_tokens)
        multi += sum(
            1
            for index in range(len(tokens) - span + 1)
            if tokens[index : index + span] == phrase_tokens
        )
    return single + multi


def _longest_pause(segments: Iterable[dict]) -> float:
    sorted_segments = sorted(segments, key=lambda segment: segment.get("start", 0.0))
    longest = 0.0
    previous_end: float | None = None
    for segment in sorted_segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if previous_end is not None:
            pause = start - previous_end
            if pause > longest:
                longest = pause
        previous_end = end
    return max(0.0, longest)


def _mean_segment_words(segments: Iterable[dict]) -> float:
    counts = [len(tokenize(segment.get("text", ""))) for segment in segments]
    counts = [count for count in counts if count > 0]
    if not counts:
        return 0.0
    return sum(counts) / len(counts)


def compute_metrics(
    text: str,
    duration_seconds: float,
    segments: list[dict] | None = None,
) -> FluencyMetrics:
    """Compute speaking-fluency metrics from a transcript + timing info.

    segments is a list of dicts with keys text/start/end (matching ASR output).
    Pass None for text-only review (pause/segment metrics return 0).
    """
    tokens = tokenize(text)
    word_count = len(tokens)
    unique = len(set(tokens))
    filler_count = _count_fillers(tokens)

    words_per_minute = (word_count / duration_seconds * 60.0) if duration_seconds > 0 else 0.0
    type_token_ratio = (unique / word_count) if word_count else 0.0
    filler_ratio = (filler_count / word_count) if word_count else 0.0

    if segments:
        longest_pause = _longest_pause(segments)
        mean_segment = _mean_segment_words(segments)
    else:
        longest_pause = 0.0
        mean_segment = 0.0

    return FluencyMetrics(
        word_count=word_count,
        unique_words=unique,
        duration_seconds=duration_seconds,
        words_per_minute=words_per_minute,
        type_token_ratio=type_token_ratio,
        filler_count=filler_count,
        filler_ratio=filler_ratio,
        mean_segment_words=mean_segment,
        longest_pause_seconds=longest_pause,
    )
