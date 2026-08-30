#!/usr/bin/env bash
# Launch llama-server tuned for GTX 1050 (4 GB VRAM, sm_61) running Qwen 3 4B Q4_K_M.
# Edit MODEL if you swap quants.
#
# There is no speculative-decoding option: measured with Qwen3-0.6B as the draft
# at n-max 4, 8 and 16, throughput was 18.04-18.17 tok/s against an 18.20 tok/s
# baseline, for 374 MiB of extra VRAM. Pascal has too little compute for the
# parallel verify to pay, so the draft model is pure cost on this card.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# NixOS: the real libcuda lives under /run/opengl-driver/lib; without it the
# nix-store CUDA stub is picked up and GPU init fails ("driver is a stub library").
if [[ -d /run/opengl-driver/lib ]]; then
  export LD_LIBRARY_PATH="/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

LLAMA_BIN="${LLAMA_BIN:-$ROOT/vendor/llama.cpp/build/bin/llama-server}"
MODEL="${MODEL:-$ROOT/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
# Qwen3-4B has 8 KV heads against Qwen2.5-3B's 2, so its KV cache costs 4x per
# token. Measured on the GTX 1050: 3069 MiB at 4096 ctx, 3357 MiB at 8192 —
# and 8192 leaves too little for faster-whisper to share the card.
CTX="${CTX:-4096}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "llama-server binary not found at $LLAMA_BIN" >&2
  echo "Build it with: nix-shell --run 'cd vendor/llama.cpp && cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_F16=ON -DGGML_CUDA_FORCE_MMQ=ON -DCMAKE_CUDA_ARCHITECTURES=61 && cmake --build build --config Release -j'" >&2
  exit 1
fi

if [[ ! -f "$MODEL" ]]; then
  echo "Model file not found: $MODEL" >&2
  echo "Download with: huggingface-cli download bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf --local-dir $ROOT/models" >&2
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

exec "$LLAMA_BIN" "${ARGS[@]}"
