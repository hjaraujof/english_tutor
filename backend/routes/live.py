"""Live conversation WebSocket: client streams 16 kHz PCM frames; server runs
VAD → ASR → LLM (streaming) → TTS, sending events back as JSON messages and
audio frames as binary.

Wire protocol (server → client):
  text frames: {"type": "transcript", "text": "..."}
                {"type": "reply_delta", "text": "..."}
                {"type": "reply_end"}
                {"type": "speech_start"}   the learner began to speak: stop any playback
                {"type": "correction", "original": "...", "corrected": "...", "reason": "..."}
                {"type": "latency", "asr_ms": ..., "llm_ms": ..., "tts_ms": ...}
                {"type": "error", "detail": "..."}
  binary frames: WAV blob (mono int16 PCM at the voice's native sample rate) TTS audio

Wire protocol (client → server):
  binary frames: mono int16 PCM mic audio at 16 kHz, any frame size — the server
                 re-chunks internally to the VAD's required window
  text frames: {"type": "end"} to close
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path

import httpx
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.analysis import compute_metrics

logger = logging.getLogger(__name__)
router = APIRouter()


SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
LIVE_REPLY_MAX_TOKENS = 256
# The chat template wraps each message in role markers that /tokenize on the
# bare content does not count.
MESSAGE_OVERHEAD_TOKENS = 8
LLM_DOWN_DETAIL = "LLM backend unavailable — is llama-server running?"
# While no speech is detected, only this many frames are retained as onset
# context (covers the VAD's speech-pad rewind); prevents unbounded growth and
# keeps idle time out of the transcribed utterance.
PRE_ROLL_FRAMES = 10


# The model does not reliably produce the documented `↪ "a" → "b" (reason)`
# shape: it substitutes typographic quotes, opens with a bare arrow instead of
# ↪, and sometimes omits the parenthetical. Accept those variants rather than
# silently dropping the correction.
_QUOTES = "\"'“”‘’"
_QUOTE = f"[{_QUOTES}]"
_ARROW = "(?:→|->|=>)"
_MARKER = f"(?:↪|{_ARROW})"

_CORRECTION_RE = re.compile(
    rf"{_MARKER}\s*{_QUOTE}([^{_QUOTES}]+){_QUOTE}"
    rf"\s*{_ARROW}\s*{_QUOTE}([^{_QUOTES}]+){_QUOTE}"
    rf"\s*(?:\(([^)]*)\))?"
)

# Any run that opens like a correction marker, whether or not it parses into a
# usable pair. The reply is spoken by the TTS, so a marker the parser could not
# read must still be removed — otherwise the learner hears the arrow and the
# parenthesis read out as words. `↪ none` is the explicit no-correction token,
# which must be stripped too.
_MARKER_SPAN_RE = re.compile(rf"{_MARKER}[ \t]*(?:{_QUOTE}|[Nn]one\b)[^\n]*")


def _split_reply_and_correction(text: str) -> tuple[str, dict | None]:
    correction = None
    match = _CORRECTION_RE.search(text)
    if match:
        original, corrected, reason = match.group(1), match.group(2), match.group(3)
        # A correction whose halves are identical renders as a correction of
        # nothing. The prompt forbids it; this guarantees it.
        if original.strip() != corrected.strip():
            correction = {
                "original": original,
                "corrected": corrected,
                "reason": (reason or "").strip(),
            }

    reply = _MARKER_SPAN_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", reply).strip(), correction


def _trim_history(
    history: list[dict[str, str]], token_counts: list[int], budget: int
) -> tuple[list[dict[str, str]], list[int]]:
    """Drop the oldest turns until the prompt fits `budget` tokens. The system
    message and the newest message always stay, and the kept turns start with
    a user message."""
    history = list(history)
    token_counts = list(token_counts)
    while (
        sum(token_counts) + MESSAGE_OVERHEAD_TOKENS * len(token_counts) > budget
        and len(history) > 2
    ):
        del history[1], token_counts[1]
        if len(history) > 2 and history[1]["role"] == "assistant":
            del history[1], token_counts[1]
    return history, token_counts


class _Conversation:
    """The turns of one live session. Each turn runs as its own task, so the
    receive loop keeps reading the microphone and can cancel a reply when the
    learner speaks over it."""

    def __init__(self, websocket: WebSocket, asr, llm, tts, system_prompt: str, system_tokens: int, budget: int) -> None:
        self.websocket = websocket
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.budget = budget
        self.history: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        self.token_counts: list[int] = [system_tokens]
        self.user_turns: list[str] = []
        self.live_corrections: list[dict] = []
        self.total_speech_seconds = 0.0
        self.learner_speaking = False
        self.closing = False
        self.reply_task: asyncio.Task | None = None
        # Turns queued behind the running one. A queued turn carries newer
        # speech, so the running turn leaves the reply to it.
        self.pending_turns = 0

    def start_turn(self, utterance: np.ndarray | None, previous: asyncio.Task | None) -> asyncio.Task:
        """Queue a turn behind `previous`. `utterance` is None when there is no
        new speech to transcribe but a reply may still be owed."""
        self.pending_turns += 1
        return asyncio.create_task(self._take_turn(utterance, previous))

    def _reply_is_owed(self) -> bool:
        return (
            self.history[-1]["role"] == "user"
            and not self.learner_speaking
            and not self.closing
            and self.pending_turns == 0
        )

    def interrupt_reply(self) -> None:
        if self.reply_task is not None:
            self.reply_task.cancel()

    async def _send(self, payload: dict) -> None:
        await self.websocket.send_text(json.dumps(payload))

    async def _append(self, role: str, content: str) -> None:
        """Add a message. A user message that follows a user message (the reply
        between them was skipped or cut before its first token) joins the
        earlier one, so roles keep alternating."""
        joins_previous = role == "user" and self.history[-1]["role"] == "user"
        if joins_previous:
            content = f'{self.history[-1]["content"]} {content}'
        tokens = await self.llm.count_tokens(content)
        if joins_previous:
            del self.history[-1], self.token_counts[-1]
        self.history.append({"role": role, "content": content})
        self.token_counts.append(tokens)

    async def _take_turn(self, utterance: np.ndarray | None, previous: asyncio.Task | None) -> None:
        if previous is not None:
            await asyncio.wait([previous])
        self.pending_turns -= 1
        asr_ms = 0
        if utterance is not None:
            try:
                asr_ms = await self._hear(utterance)
            except Exception as exc:  # noqa: BLE001 — a failed turn must not end the session
                await self._report_failure(exc)
        # The user message may come from an earlier turn whose reply was
        # skipped, so the check is on the history, not on this utterance.
        if not self._reply_is_owed():
            return
        self.reply_task = asyncio.current_task()
        try:
            await self._reply(asr_ms)
        except Exception as exc:  # noqa: BLE001 — a failed turn must not end the session
            await self._report_failure(exc)
        finally:
            self.reply_task = None

    async def _hear(self, utterance: np.ndarray) -> int:
        """Transcribe one utterance into the history. Returns the ASR time in ms."""
        asr_started = time.perf_counter()
        transcription = await _transcribe_pcm(self.asr, utterance)
        asr_ms = int((time.perf_counter() - asr_started) * 1000)
        if transcription.strip():
            self.user_turns.append(transcription)
            self.total_speech_seconds += utterance.size / SAMPLE_RATE
            await self._append("user", transcription)
            await self._send({"type": "transcript", "text": transcription})
        return asr_ms

    async def _report_failure(self, exc: Exception) -> None:
        logger.error("live turn failed", exc_info=exc)
        detail = LLM_DOWN_DETAIL if isinstance(exc, httpx.HTTPError) else str(exc)
        with contextlib.suppress(Exception):
            await self._send({"type": "error", "detail": detail})

    async def _reply(self, asr_ms: int) -> None:
        self.history, self.token_counts = _trim_history(self.history, self.token_counts, self.budget)
        llm_started = time.perf_counter()
        accumulated = ""
        try:
            async for delta in self.llm.chat_stream(self.history, max_tokens=LIVE_REPLY_MAX_TOKENS):
                accumulated += delta
                await self._send({"type": "reply_delta", "text": delta})
        except asyncio.CancelledError:
            if accumulated:
                await self._append("assistant", accumulated)
            # The socket is already gone when the cancel comes from a disconnect.
            with contextlib.suppress(Exception):
                await self._send({"type": "reply_end"})
            raise
        llm_ms = int((time.perf_counter() - llm_started) * 1000)

        if accumulated:
            await self._append("assistant", accumulated)
        reply, correction = _split_reply_and_correction(accumulated)
        await self._send({"type": "reply_end"})
        if correction:
            await self._send({"type": "correction", **correction})
            self.live_corrections.append(correction)

        tts_ms = 0
        if self.tts is not None and reply and self.tts.is_available():
            tts_started = time.perf_counter()
            wav = await self.tts.synthesize(reply)
            tts_ms = int((time.perf_counter() - tts_started) * 1000)
            if wav:
                await self.websocket.send_bytes(wav)

        await self._send({"type": "latency", "asr_ms": asr_ms, "llm_ms": llm_ms, "tts_ms": tts_ms})


async def _review_live_session(llm, config, conversation: _Conversation) -> dict:
    """Grammar review of everything the learner said, so a live session lands
    in the history with the same typed corrections as a recording. The
    corrections the tutor spoke during the conversation stay beside it under
    their own key."""
    turn_count = len(conversation.user_turns)
    try:
        review = await llm.review(
            transcript=" ".join(conversation.user_turns),
            native_language=config.user.native_language,
            cefr_level=config.user.cefr_level,
        )
        review_dump = review.model_dump(by_alias=True)
    except httpx.HTTPError as exc:
        logger.warning("live session review failed: %s", exc)
        review_dump = {
            "corrections": [],
            "fluency_notes": [],
            "vocabulary_suggestions": [],
            "overall": {
                "summary": (
                    f"Live conversation: {turn_count} spoken turn(s). "
                    "The grammar review did not run because llama-server did not answer."
                ),
                "strengths": [],
                "next_focus": [],
            },
        }
    review_dump["live_corrections"] = conversation.live_corrections
    return review_dump


@router.websocket("/ws/live")
async def live_socket(websocket: WebSocket):
    await websocket.accept()
    state = websocket.app.state
    config = state.config
    asr = state.asr
    llm = state.llm
    db = state.db
    tts = getattr(state, "tts", None)

    if getattr(state, "live_active", False):
        await websocket.send_text(json.dumps({
            "type": "error",
            "detail": "another live conversation is already active",
        }))
        await websocket.close()
        return
    state.live_active = True

    conversation: _Conversation | None = None
    turn_task: asyncio.Task | None = None

    try:
        try:
            from silero_vad import load_silero_vad, VADIterator
        except ImportError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "detail": "silero-vad not installed. Run: uv sync --extra phase2",
            }))
            return

        # torch.jit.load blocks for ~a second: run it off the event loop, once
        # per process. The shared model carries streaming state, so the
        # live_active guard above keeps live connections exclusive.
        vad_model = getattr(state, "vad_model", None)
        if vad_model is None:
            vad_model = await asyncio.to_thread(load_silero_vad)
            state.vad_model = vad_model
        vad = VADIterator(
            vad_model,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=config.live.silence_end_ms,
        )

        prompt_path = Path(config.project_root) / "backend" / "prompts" / "conversation_partner.md"
        system_prompt = (await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")).format(
            native_language=config.user.native_language,
            cefr_level=config.user.cefr_level,
        )
        try:
            context_tokens = await llm.context_size()
            system_tokens = await llm.count_tokens(system_prompt)
        except httpx.HTTPError as exc:
            logger.warning("live conversation could not reach the LLM: %s", exc)
            await websocket.send_text(json.dumps({"type": "error", "detail": LLM_DOWN_DETAIL}))
            return
        conversation = _Conversation(
            websocket, asr, llm, tts,
            system_prompt=system_prompt,
            system_tokens=system_tokens,
            budget=context_tokens - LIVE_REPLY_MAX_TOKENS,
        )

        audio_buffer: list[np.ndarray] = []
        pcm_residual = np.empty(0, dtype=np.float32)
        in_speech = False
        speech_start_time: float | None = None

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message and message["text"]:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "end":
                    break
                continue

            data = message.get("bytes")
            if not data or len(data) % 2 != 0:
                continue

            incoming = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if incoming.size == 0:
                continue
            pcm_residual = np.concatenate([pcm_residual, incoming])

            while pcm_residual.size >= FRAME_SAMPLES:
                frame = pcm_residual[:FRAME_SAMPLES]
                pcm_residual = pcm_residual[FRAME_SAMPLES:]

                audio_buffer.append(frame)
                if not in_speech and len(audio_buffer) > PRE_ROLL_FRAMES:
                    del audio_buffer[0]

                vad_event = vad(frame)
                if vad_event:
                    if "start" in vad_event and not in_speech:
                        in_speech = True
                        speech_start_time = time.perf_counter()
                        conversation.learner_speaking = True
                        conversation.interrupt_reply()
                        await websocket.send_text(json.dumps({"type": "speech_start"}))
                    if "end" in vad_event and in_speech:
                        utterance = np.concatenate(audio_buffer)
                        audio_buffer = []
                        in_speech = False
                        conversation.learner_speaking = False
                        if speech_start_time is None:
                            continue
                        turn_task = conversation.start_turn(utterance, turn_task)

                if in_speech and speech_start_time is not None:
                    if time.perf_counter() - speech_start_time > config.live.max_utterance_seconds:
                        audio_buffer = []
                        in_speech = False
                        speech_start_time = None
                        conversation.learner_speaking = False
                        # VADIterator still holds triggered state after a forced
                        # cut; without a reset its next 'end' event would be
                        # dropped and the overflow would bleed into the next turn.
                        vad.reset_states()
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": "Utterance too long; cut off.",
                        }))
                        # The cut speech interrupted or held back a reply that
                        # no later turn will now send.
                        turn_task = conversation.start_turn(None, turn_task)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures
        logger.exception("live socket error")
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": str(exc)}))
        except Exception:
            pass
    finally:
        state.live_active = False
        if conversation is not None:
            conversation.closing = True
            conversation.interrupt_reply()
            if turn_task is not None:
                await asyncio.wait([turn_task])
        if conversation is not None and conversation.user_turns:
            transcript_text = " ".join(conversation.user_turns)
            metrics = compute_metrics(
                text=transcript_text, duration_seconds=conversation.total_speech_seconds
            )
            try:
                review_dump = await _review_live_session(llm, config, conversation)
                await asyncio.to_thread(
                    lambda: db.insert_session(
                        kind="live",
                        transcript=transcript_text,
                        review=review_dump,
                        metrics=metrics.to_dict(),
                    )
                )
            except Exception:
                logger.exception("failed to persist live session")
        try:
            await websocket.close()
        except Exception:
            pass


async def _transcribe_pcm(asr, samples: np.ndarray) -> str:
    """Write a temp wav and let faster-whisper read it. Trades a tiny amount of
    latency for code simplicity vs. plumbing in-memory ndarrays through CTranslate2.
    """
    import io
    import wave

    buffer = io.BytesIO()
    int16_samples = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(int16_samples.tobytes())
    buffer.seek(0)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as temp_file:
        temp_file.write(buffer.getvalue())
        temp_file.flush()
        transcription = await asyncio.to_thread(asr.transcribe, Path(temp_file.name))
    return transcription.text
