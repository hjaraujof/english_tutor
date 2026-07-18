# english_tutor

Local English tutor: grammar review (writing) + speaking-fluency feedback
(record-and-review and live conversation). 100% local — no data leaves the
machine.

Stack:
- **LLM**: Qwen 2.5 3B Instruct (GGUF Q4_K_M) served by `llama-server` from
  llama.cpp built locally with CUDA for Pascal (sm_61).
- **ASR**: `faster-whisper` (`small.en`, int8_float16, CUDA).
- **TTS** (Phase 2): Piper, CPU.
- **Backend**: FastAPI + SQLite.
- **Frontend**: vanilla HTML/JS (no build step).

## Hardware target

Tuned for a single-GPU box with ~4 GB VRAM (e.g. GTX 1050). VRAM budget at
8K context with KV cache q8_0:
- Qwen 2.5 3B Q4_K_M weights: ~1.9 GB
- KV cache: ~250 MB
- Whisper small.en int8_float16: ~600 MB
- CUDA context + headroom: ~400 MB

## One-time setup

```bash
# Tools
mise use python@3.13 uv@latest
sudo nix-channel --update    # for nix-shell to pick up cudatoolkit

# 1. Clone + build llama.cpp with CUDA (sm_61). Roughly 10–20 minutes.
#    Pinned: this commit is the one verified against start_llm.sh's flags; upstream
#    later reworked the speculative-decoding CLI (-md/-ngld/--draft → --spec-type).
mkdir -p vendor/llama.cpp && git -C vendor/llama.cpp init -q && \
  git -C vendor/llama.cpp fetch --depth 1 https://github.com/ggerganov/llama.cpp \
      b97ebdc98f6053604a19d861c08d8087601b96e0 && \
  git -C vendor/llama.cpp checkout -q FETCH_HEAD
nix-shell shell.nix --run "cd vendor/llama.cpp && \
  cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_F16=ON -DGGML_CUDA_FORCE_MMQ=ON \
                  -DCMAKE_CUDA_ARCHITECTURES=61 -DLLAMA_BUILD_SERVER=ON \
                  -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF && \
  cmake --build build --config Release -j --target llama-server llama-bench"

# 2. Download the model (~1.8 GB).
uvx --from huggingface_hub hf download bartowski/Qwen2.5-3B-Instruct-GGUF \
    Qwen2.5-3B-Instruct-Q4_K_M.gguf --local-dir models

# 3. Python deps.
uv sync --extra dev
# Add Phase-2 deps when you're ready for live conversation:
#   uv sync --extra dev --extra phase2

# 4. (Phase 2 only) Piper voice — install piper via nix or pip, then drop the
#    voice model under models/piper/en_US-amy-medium.onnx (+ .onnx.json).
```

## Running

Two processes:

```bash
# Terminal 1: LLM server
./start_llm.sh

# Terminal 2: backend + frontend. Pass the extras — a plain `uv run` syncs the
# default (no-extras) set and would uninstall the phase2 packages.
uv run --extra phase2 uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

Then open <http://127.0.0.1:8765>.

## Layout

```
backend/
  main.py            FastAPI composition (lifespan loads ASR + LLM + DB once)
  asr.py             faster-whisper wrapper
  llm.py             OpenAI-compat client → llama-server, JSON-schema parsing
  tts.py             Piper subprocess wrapper (Phase 2)
  analysis.py        Pure-Python fluency metrics: WPM, TTR, filler ratio, pauses
  db.py              SQLite: sessions, errors, metric trends
  config.py          Loads config.toml at startup
  routes/
    sessions.py      POST /api/sessions     audio → ASR → review → metrics → DB
    review.py        POST /api/review       text-only path (writing review)
    live.py          WS  /ws/live           Phase 2: VAD + streaming LLM + TTS
  prompts/
    grammar_review.md           Structured-JSON review prompt
    conversation_partner.md     Phase 2 tutor persona + correction style
frontend/
  index.html         Tabs: Record / Paste text / History / Converse
  app.js             MediaRecorder + WebSocket conversation
  app.css
tests/
  conftest.py            TestClient app fixture: FakeASR/FakeLLM + tmp SQLite
  test_analysis.py       Fluency-metric unit tests (no LLM)
  test_db.py             SQLite round-trip, FK cascade, trend queries
  test_live.py           History trim + live-WS malformed-frame guards
  test_llm_parse.py      Review-JSON fallback + correction-marker parsing
  test_llm_prompts.py    Integration; runs only with `-m integration`
  test_routes.py         Upload/review routes, orphan cleanup, trend filter
  test_tts.py            Piper WAV contract (stub binary)
data/                  Audio + tutor.db (gitignored)
models/                GGUFs, Piper voices (gitignored)
vendor/llama.cpp/      Local clone (gitignored)
```

## Configuration (`config.toml`)

| Key | Notes |
|---|---|
| `user.native_language`, `user.cefr_level` | Drives L1-interference targeting in the prompt. |
| `llm.base_url` | Default `http://127.0.0.1:8080`. |
| `asr.model_size` | `tiny.en` for lower latency, `small.en` for accuracy. |
| `asr.compute_type` | `int8_float16` on CUDA, `int8` on CPU fallback. |

## Verification

```bash
# Smoke (audio path):
curl -F "audio=@sample.webm" http://127.0.0.1:8765/api/sessions

# Smoke (text path):
curl -d '{"text":"He go to school yesterday."}' \
     -H 'content-type: application/json' \
     http://127.0.0.1:8765/api/review

# Tests:
uv run --extra dev pytest                        # unit only (no server)
uv run --extra dev pytest -m integration         # requires llama-server running
```

## Tuning notes

- **VRAM tight?** Drop the 3B to IQ4_XS (~1.7 GB) or shrink `-c` from 8192 to 4096.
- **Need lower live-conversation latency?** Switch ASR to `tiny.en`, enable
  speculative decoding (`DRAFT=1` in `start_llm.sh`), or stream LLM tokens to
  Piper sentence-by-sentence.
- **Pre-warm**: llama-server reuses each slot's prompt cache across turns, so
  multi-turn live mode benefits a lot from reusing the same WS session
  (`--slots` in start_llm.sh only exposes the monitoring endpoint).

## Out of scope (deliberately deferred)

- CEFR-level auto-assessment beyond a per-session estimate.
- Spaced-repetition deck generation from recurring errors.
- Multi-user / auth.
- Pronunciation phoneme scoring.
