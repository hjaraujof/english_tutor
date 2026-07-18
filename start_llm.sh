#!/usr/bin/env bash
# Launch llama-server tuned for GTX 1050 (4 GB VRAM, sm_61) running Qwen 2.5 3B Q4_K_M.
# Edit MODEL / DRAFT_MODEL paths if you swap quants. Toggle DRAFT=1 to enable
# speculative decoding once you've confirmed acceptance rate is worth it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# NixOS: the real libcuda lives under /run/opengl-driver/lib; without it the
# nix-store CUDA stub is picked up and GPU init fails ("driver is a stub library").
if [[ -d /run/opengl-driver/lib ]]; then
  export LD_LIBRARY_PATH="/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

LLAMA_BIN="${LLAMA_BIN:-$ROOT/vendor/llama.cpp/build/bin/llama-server}"
MODEL="${MODEL:-$ROOT/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf}"
DRAFT_MODEL="${DRAFT_MODEL:-$ROOT/models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
CTX="${CTX:-8192}"
DRAFT="${DRAFT:-0}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "llama-server binary not found at $LLAMA_BIN" >&2
  echo "Build it with: nix-shell --run 'cd vendor/llama.cpp && cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_F16=ON -DGGML_CUDA_FORCE_MMQ=ON -DCMAKE_CUDA_ARCHITECTURES=61 && cmake --build build --config Release -j'" >&2
  exit 1
fi

if [[ ! -f "$MODEL" ]]; then
  echo "Model file not found: $MODEL" >&2
  echo "Download with: huggingface-cli download bartowski/Qwen2.5-3B-Instruct-GGUF Qwen2.5-3B-Instruct-Q4_K_M.gguf --local-dir $ROOT/models" >&2
  exit 1
fi

ARGS=(
  -m "$MODEL"
  --host "$HOST"
  --port "$PORT"
  -ngl 99
  -c "$CTX"
  -fa on
  -ctk q8_0 -ctv q8_0
  --mlock
  -b 512 -ub 512
  --slots
)

if [[ "$DRAFT" == "1" ]]; then
  if [[ ! -f "$DRAFT_MODEL" ]]; then
    echo "Draft model enabled but not found: $DRAFT_MODEL" >&2
    exit 1
  fi
  ARGS+=(-md "$DRAFT_MODEL" -ngld 99 --draft 8)
fi

exec "$LLAMA_BIN" "${ARGS[@]}"
