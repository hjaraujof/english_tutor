# english_tutor

Local English tutor: grammar review (writing) + speaking-fluency feedback
(record-and-review and live conversation). 100% local — no data leaves the
machine.

Stack:
- **LLM**: Qwen 3 4B Instruct 2507 (GGUF Q4_K_M) served by `llama-server` from
  llama.cpp built locally with CUDA for Pascal (sm_61).
- **ASR**: `faster-whisper` (`distil-small.en`, int8, CUDA).
- **TTS** (Phase 2): Piper, CPU.
- **Backend**: FastAPI + SQLite.
- **Frontend**: vanilla HTML/JS (no build step).

## Hardware target

Tuned for a single-GPU box with ~4 GB VRAM (e.g. GTX 1050, 4031 MiB usable).
Measured with `nvidia-smi` at 4K context with KV cache q8_0:
- `llama-server` with Qwen 3 4B Q4_K_M, weights + KV + CUDA context: 3051 MiB
- `distil-small.en` int8: 278 MiB
- Both loaded at once: 3329 MiB, leaving 702 MiB headroom

Context is 4096, not 8192. Qwen 3 4B has 8 KV heads where Qwen 2.5 3B had 2, so
its KV cache costs 4x per token. `CTX=8192` also fits (3635 MiB combined, 396 MiB
headroom) if live-conversation history needs the room.

`compute_type` must be `int8`, not `int8_float16`. Pascal (sm_61) has no
efficient FP16, so ctranslate2 offers only `{int8, int8_float32, float32}` on
CUDA and raises `ValueError` for anything else — which `backend/asr.py` turns
into a silent CPU fallback that costs ~8x (0.6x realtime against 5.2x).

## One-time setup

```bash
# Tools
mise use python@3.13 uv@latest
sudo nix-channel --update    # for nix-shell to pick up the CUDA components

# 1. Clone + build llama.cpp with CUDA (sm_61). Roughly 10–20 minutes.
#    The commit is pinned because it is the one verified against start_llm.sh's
#    flags. shell.nix requests four CUDA components rather than the merged
#    `cudatoolkit`; see the note under Tuning before you widen that list.
mkdir -p vendor/llama.cpp && git -C vendor/llama.cpp init -q && \
  git -C vendor/llama.cpp fetch --depth 1 https://github.com/ggerganov/llama.cpp \
      b97ebdc98f6053604a19d861c08d8087601b96e0 && \
  git -C vendor/llama.cpp checkout -q FETCH_HEAD
nix-shell shell.nix --run "cd vendor/llama.cpp && \
  cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_F16=ON -DGGML_CUDA_FORCE_MMQ=ON \
                  -DCMAKE_CUDA_ARCHITECTURES=61 -DLLAMA_BUILD_SERVER=ON \
                  -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF && \
  cmake --build build --config Release -j --target llama-server llama-bench"

# 2. Download the model (~2.4 GB).
uvx --from huggingface_hub hf download bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF \
    Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf --local-dir models
mv models/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
   models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf

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

# Terminal 2: backend + frontend. Use the script — it puts libcublas.so.12 on
# LD_LIBRARY_PATH (without it faster-whisper silently drops to CPU) and it
# activates .venv rather than `uv run`, which would re-sync the default extras
# and uninstall the phase2 packages.
./start_backend.sh
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
| `asr.model_size` | `distil-small.en`. `tiny.en` is 3x faster but transcribes learner errors less faithfully. |
| `asr.compute_type` | `int8` on both CUDA and CPU. `int8_float16` is rejected on Pascal. |

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

- **`nix-shell` fails with "No space left on device"?** Do not reach for
  `cuda.cudatoolkit` to fix it — that is the cause. It is the merged runfile
  package and pulls cuFFT, cuSOLVER, cuSPARSE and NPP, none of which ggml-cuda
  references, and the merge derivation needs more scratch than this disk has.
  `shell.nix` requests four components instead: `cuda_nvcc`, `cuda_cudart`,
  `libcublas`, `cuda_cccl`. That set builds `ggml-cuda` cleanly and costs ~4.4 GB
  against the 11 GB the merged build exhausted before failing.
- **VRAM tight?** Drop the 4B to IQ4_XS (~2.2 GB) or shrink `-c` from 4096 to 2048.
- **`nix-shell` fails with "No space left on device"?** Check free disk before
  suspecting the channel. `shell.nix` asks for four CUDA components —
  `cuda_nvcc`, `cuda_cudart`, `libcublas`, `cuda_cccl` — which is what
  ggml-cuda actually references, and costs ~4.4 GB. The merged
  `cudaPackages.cudatoolkit` also pulls cuFFT, cuSOLVER, cuSPARSE and NPP, and
  its merge derivation needs more scratch than this disk can spare. Adding a
  component is fine; switching back to `cudatoolkit` will fail here.
- **ASR suddenly slow?** It fell back to CPU. `distil-small.en` runs at 5.2x
  realtime on CUDA and 0.6x on CPU. Check the backend log for the CUDA warning
  from `backend/asr.py`, then confirm `libcublas.so.12` is on `LD_LIBRARY_PATH`.
- **Need lower live-conversation latency?** Switch ASR to `tiny.en` (13.5x
  realtime, 115 MiB, but less faithful to learner errors), or stream LLM tokens
  to Piper sentence-by-sentence. Speculative decoding is not an option here: a
  Qwen3-0.6B draft measured 18.04–18.17 tok/s at n-max 4/8/16 against an
  18.20 tok/s baseline, and cost 374 MiB. Pascal has too little compute for the
  parallel verify to pay for itself.
- **Pre-warm**: llama-server reuses each slot's prompt cache across turns, so
  multi-turn live mode benefits a lot from reusing the same WS session
  (`--slots` in start_llm.sh only exposes the monitoring endpoint).

## Out of scope (deliberately deferred)

- CEFR-level auto-assessment beyond a per-session estimate.
- Spaced-repetition deck generation from recurring errors.
- Multi-user / auth.
- Pronunciation phoneme scoring.
