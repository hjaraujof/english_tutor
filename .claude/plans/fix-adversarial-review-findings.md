# Plan: Fix adversarial-review findings (F1–F6)

## Context

The initial scaffold commit (`63b568c`) for the local English tutor was put through an
adversarial code review. Six findings were validated against source. No-CORS/no-auth was
explicitly **deferred by the user** and is out of scope here — the documented threat model is
localhost single-user, so the real risks are correctness/operability defects that break the
single user's own sessions, plus two trust-boundary input bugs.

This plan fixes all six. User decisions (captured via question prompt):
- **F1**: offload **both** ASR and DB calls off the event loop.
- **F2**: **coerce** disallowed upload extensions to `.webm` (do not 400).
- **F5**: **uncap** trends (SQL-side `json_extract`, ASC order).
- **F6**: add the **full** test set.

All work obeys the workspace Golden Rules: surgical edits, no placeholders, no temporal
comments, run tests after every change.

---

## Findings → fixes

### F1 — Blocking ASR/SQLite on the event loop (high)
`asr.transcribe` is sync (asr.py:73) and called directly from `async def` handlers, blocking
uvicorn's single loop for the full multi-second transcription; every other HTTP/WS request
stalls. DB calls are also sync-on-loop.

**Fix — wrap blocking calls in `asyncio.to_thread`:**
- `backend/routes/sessions.py:48` — `transcription = await asyncio.to_thread(asr.transcribe, audio_path)`
- `backend/routes/sessions.py:76` — `session_id = await asyncio.to_thread(db.insert_session, kind="audio", ...)` (keyword args preserved via a `functools.partial` or a lambda; `to_thread` forwards `*args, **kwargs`, so `await asyncio.to_thread(lambda: db.insert_session(...))` or pass kwargs directly to `to_thread`).
- `backend/routes/sessions.py:107,112,117` — wrap `db.list_sessions`, `db.recurring_error_types`, `db.metric_trend` reads.
- `backend/routes/review.py:27` is already `await llm.review(...)` (async, fine); wrap `review.py:35` `db.insert_session` in `to_thread`.
- `backend/routes/live.py:207` — inside `_transcribe_pcm`, `transcription = await asyncio.to_thread(asr.transcribe, Path(temp_file.name))`. Note `_transcribe_pcm` is already `async`; the `NamedTemporaryFile` context must stay open across the await (it will — the `with` block encloses the await).
- Add `import asyncio` where missing (`sessions.py`, `review.py`; `live.py` already imports it transitively? verify — it imports `time`/`json`, not `asyncio`, so add it).

**Thread-safety note:** `Database._connect` (db.py:46) opens a **fresh** `sqlite3.connect` per call
and closes it in `finally`, so no connection is shared across threads — `to_thread` is safe.
Do **not** add a shared connection.

### F2 — Attacker-controlled upload filename → 500 + garbage filenames (high)
`suffix = Path(audio.filename or "audio.webm").suffix or ".webm"` (sessions.py:36) lets the
client inject an unbounded suffix with newlines/control chars; the file write (sessions.py:43)
is **outside** the try/except (sessions.py:47), so a too-long name raises `OSError` → 500.

**Fix — whitelist + coerce, in `backend/routes/sessions.py`:**
- Add a module constant `ALLOWED_AUDIO_SUFFIXES = {".webm", ".wav", ".ogg", ".mp3", ".m4a"}`.
- Replace the suffix line with: take `Path(audio.filename or "").suffix.lower()`, and if it's
  not in the whitelist, coerce to `".webm"`. This bounds length and strips control chars by
  construction (any garbage extension → `.webm`).
- Move the file write (sessions.py:43-44) **inside** a `try/except OSError`, raising a clean
  `HTTPException(status_code=400, detail="could not store uploaded audio")` and unlinking any
  partial file on failure. (This dovetails with F3's cleanup.)

### F3 — Orphaned/partial audio files on failure (medium)
The audio file is written before transcription; if ASR raises (→500) or the client disconnects
mid-copy, the file is left on disk untracked, with no cleanup. `data/audio/` grows unboundedly.

**Fix — temp-write-then-rename + cleanup, in `backend/routes/sessions.py`:**
- Write to a temp path in the same `data/audio/` dir (e.g. `audio_path.with_suffix(suffix + ".part")`
  or a `tempfile.NamedTemporaryFile(dir=audio_dir, delete=False)`).
- After successful `asr.transcribe`, `os.replace(temp, audio_path)` into the final location.
- On **any** exception during write or transcription, `unlink` the temp/partial file (wrap in a
  `try/except FileNotFoundError`) and log one line, then re-raise as `HTTPException`.
- Net effect: the final `data/audio/<ts>_<id>.<ext>` only ever exists for a fully-transcribed
  session that is about to get a DB row.

### F4 — Live socket dies on one bad text frame; unbounded history (medium)
`json.loads(message["text"])` (live.py:100) is unguarded; a single non-JSON frame raises and is
caught by the broad `except` (live.py:173) which closes the whole conversation. Separately,
`history` (live.py:132,142) grows without bound, eventually overflowing the 8K context.

**Fix — in `backend/routes/live.py`:**
- Guard the text-frame parse: wrap `json.loads(message["text"])` in
  `try: payload = json.loads(...) except json.JSONDecodeError: continue` so a malformed frame is
  ignored instead of killing the socket.
- Cap history after each assistant append. Add a module constant `MAX_HISTORY_TURNS = 12` and,
  after `history.append({"role": "assistant", ...})` (live.py:142), trim while **always**
  preserving the system message at index 0:
  `if len(history) > 1 + 2 * MAX_HISTORY_TURNS: history = [history[0]] + history[-2 * MAX_HISTORY_TURNS:]`.

### F5 — `metric_trend` caps at newest 50 rows before filtering (medium)
`metric_trend` does `ORDER BY id DESC LIMIT 50` (db.py:144) then filters in Python (db.py:150),
so trend charts only ever reflect the newest 50 sessions regardless of history length — wrong for
the app's core "progress over time" feature. The frontend also calls `.points.map(...)` without a
`|| []` guard on the filler/TTR responses (app.js:239-240), so a bad shape throws and the catch
(app.js:254) wipes the whole History render.

**Fix:**
- `backend/db.py:141-153` — rewrite `metric_trend` to push the key filter into SQL and drop the
  cap: `SELECT id, created_at, json_extract(metrics_json, '$.' || ?) AS value FROM sessions
  WHERE value IS NOT NULL ORDER BY id ASC` with param `(key,)`. Returns full history, oldest→newest
  (no Python `reverse()` needed). Keep the method signature compatible — drop the `limit` param or
  default it to `None` and ignore; simpler to remove it and update the one caller.
- `backend/routes/sessions.py:115-117` — drop the `limit` query param from `metric_trend` route
  (it no longer applies), or keep the route param but stop forwarding it. Recommend removing it for
  honesty.
- `frontend/app.js:239-240` — add `|| []` guards mirroring line 238:
  `((await fillerResponse.json()).points || []).map(...)` and same for TTR.

> Note: `compute_metrics().to_dict()` (analysis.py:32-43) always emits all 9 keys, so the
> `json_extract` filter matches every row that has metrics — correct for both audio and text
> sessions.

### F6 — No failure-mode test coverage (low)
The 7 existing unit tests (test_analysis.py) cover only pure metric math; the LLM integration
tests (test_llm_prompts.py) skip without a live server. Zero coverage of routes, DB, parse
fallback, or the correction regex.

**Fix — add `tests/conftest.py` + new test files. Stub `asr`/`llm`; use real SQLite on `tmp_path`.**

App wiring (from `backend/main.py:61`): `FastAPI(..., lifespan=lifespan)`; state set at lifespan
lines 49-53 as `app.state.{config,asr,llm,db,tts}`. Config is frozen dataclasses (config.py);
`load_config(path)` accepts an override path.

- **`tests/conftest.py`** (new):
  - A fixture building a `FastAPI()` app that `include_router`s `sessions`, `review`, `live`
    routers, then sets `app.state.config` (a real `load_config()` pointed at a tmp config, or the
    repo `config.toml`), `app.state.db = Database(tmp_path / "t.db")`, `app.state.asr = FakeASR()`,
    `app.state.llm = FakeLLM()`, `app.state.tts = None`. Yield a `TestClient(app)`.
  - `FakeASR.transcribe(path)` returns a canned `Transcription` (import the real dataclass from
    `backend.asr`) with one segment.
  - `FakeLLM` is an `async` stub: `async def review(...)` returns a real `Review` (from
    `backend.llm`); it needs no network. `to_thread`-wrapped DB calls work unchanged against the
    real `Database`.
  - Register `asyncio_mode` or keep `@pytest.mark.asyncio` per-test (repo currently uses the
    marker; add `asyncio_mode = "auto"` to `[tool.pytest.ini_options]` to simplify — optional).
- **`tests/test_routes.py`** (new):
  - `POST /api/review` with valid text → 200, body has `review`/`metrics`/`id`, and a row landed in
    the DB (assert via `app.state.db.list_sessions`).
  - `POST /api/sessions` with a multipart upload whose `filename` has a **garbage/oversized
    extension** → 200 (coerced to `.webm`), and the stored file under `data/audio/` ends in
    `.webm` (F2 regression). Use `FakeASR` so no real model loads.
  - `POST /api/sessions` where `FakeASR.transcribe` raises → 500 **and** no orphan file remains in
    `data/audio/` (F3 regression).
- **`tests/test_db.py`** (new, real SQLite on `tmp_path`):
  - insert→list round-trip; review corrections fan out into `errors`; FK `ON DELETE CASCADE`
    deletes child errors when a session is deleted.
  - **>50-row trend**: insert 60 sessions, assert `metric_trend("words_per_minute")` returns 60
    points in ascending `id` order (F5 regression).
  - `recurring_error_types` counts/threshold behavior.
- **`tests/test_llm_parse.py`** (new, no network):
  - `LLMClient._parse_review("not json")` → returns the fallback `Review` with the
    "could not be parsed" summary (the one safety branch, llm.py:155-165).
  - `_split_reply_and_correction` (live.py:43) on a reply containing the `↪ "x" → "y" (reason)`
    marker returns the parsed correction dict; on plain text returns `(text, None)`.

---

## Critical files

| File | Findings | Change |
|------|----------|--------|
| `backend/routes/sessions.py` | F1, F2, F3, F5 | offload ASR+DB; extension whitelist+coerce; temp-write+cleanup; drop trend `limit` |
| `backend/routes/review.py` | F1 | offload `db.insert_session` |
| `backend/routes/live.py` | F1, F4 | offload ASR; guard text-frame JSON; cap history |
| `backend/db.py` | F5 | rewrite `metric_trend` (json_extract, ASC, uncapped) |
| `frontend/app.js` | F5 | `|| []` guards on filler/TTR `.points` |
| `tests/conftest.py` | F6 | new — TestClient app fixture with FakeASR/FakeLLM + tmp SQLite |
| `tests/test_routes.py` | F6 | new — route + F2/F3 regression tests |
| `tests/test_db.py` | F6 | new — DB round-trip, FK cascade, >50-row trend (F5) |
| `tests/test_llm_parse.py` | F6 | new — `_parse_review` fallback + correction regex |
| `pyproject.toml` | F6 | (optional) `asyncio_mode = "auto"` |

**Reused, do not reinvent:** `compute_metrics` (analysis.py:83), `Transcription`/`Segment`/`Word`
dataclasses (asr.py:19-50), `Review`/`OverallFeedback` models + `_parse_review` (llm.py),
`Database` (db.py), `load_config` (config.py), the `integration` marker + `pytest-asyncio` already
in `pyproject.toml`.

---

## Verification

1. **Unit/integration tests** (no server, no GPU needed — ASR/LLM are stubbed):
   ```bash
   source .venv/bin/activate
   python3 -m pytest tests/ -v
   ```
   Expect the existing 7 analysis tests plus the new route/DB/parse tests to pass; the
   llama-server integration tests stay auto-skipped.

2. **F1 manual smoke** (optional, needs the real app): start the server, open two browser tabs,
   trigger an audio review in one while polling `/api/health` in the other — health must respond
   immediately rather than blocking for the transcription duration.

3. **F2/F3**: covered by `test_routes.py` (coerced `.webm` filename; no orphan on ASR failure). No
   manual step required.

4. **F4 manual smoke**: with live mode running, send a non-JSON text frame over `/ws/live` — the
   socket must stay open (frame ignored), not disconnect. Hold a long conversation and confirm
   history stays bounded (server logs / no context-overflow error from llama-server).

5. **F5**: `test_db.py` asserts >50-point ascending trends; in the UI, the History tab charts
   should render across full history and survive a malformed trend response (no blank History).

6. **Full suite green** is the completion bar — a change without a passing `pytest` run is
   incomplete (workspace rule).
