#!/usr/bin/env bash
# Launch the FastAPI backend with CUDA visible to faster-whisper.
#
# Two libraries must be on the path before ctranslate2 can use the GPU, and
# neither is there by default:
#   libcuda.so    - the real driver, under /run/opengl-driver/lib on NixOS.
#   libcublas.so.12 - from the pinned cuda-merged-12.6 closure. Without it
#                   ctranslate2 raises "Library libcublas.so.12 is not found"
#                   at the first encode, and backend/asr.py falls back to CPU.
#
# The CPU fallback is silent and costs ~8x: distil-small.en measures 5.2x
# realtime on this GTX 1050 against 0.6x on CPU.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"

if [[ -d /run/opengl-driver/lib ]]; then
  export LD_LIBRARY_PATH="/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# The cuda-merged GC root is committed under .nix-gc-roots, so this path is
# stable across garbage collection without re-entering nix-shell.
CUDA_LIB=$(echo "$ROOT"/.nix-gc-roots/*-cuda-merged-*/lib)
if [[ -f "$CUDA_LIB/libcublas.so.12" ]]; then
  export LD_LIBRARY_PATH="$CUDA_LIB:$LD_LIBRARY_PATH"
else
  echo "libcublas.so.12 not found under .nix-gc-roots/*-cuda-merged-*/lib" >&2
  echo "ASR will fall back to CPU (~8x slower). Restore the GC root with:" >&2
  echo "  nix-shell shell.nix --run true" >&2
fi

if [[ ! -x "$ROOT/.venv/bin/uvicorn" ]]; then
  echo "No .venv found. Create it with: uv sync --extra dev --extra phase2" >&2
  exit 1
fi

# Activate rather than `uv run`: a plain `uv run` re-syncs the default extras
# and uninstalls the phase2 packages the live-conversation path needs.
source "$ROOT/.venv/bin/activate"

exec uvicorn backend.main:app --host "$HOST" --port "$PORT"
