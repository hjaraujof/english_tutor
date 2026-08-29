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

# shell.nix roots each CUDA component's `lib` output under .nix-gc-roots, so
# these paths survive nix-collect-garbage without re-entering the shell. Match
# CUDA roots by name rather than globbing every rooted lib dir: .nix-gc-roots
# also holds glibc, and putting the nix glibc ahead of the system one kills the
# interpreter outright with "undefined symbol: __nptl_change_stack_perm".
found_cublas=0
for lib_dir in "$ROOT"/.nix-gc-roots/*cuda*/lib "$ROOT"/.nix-gc-roots/*cublas*/lib; do
  [[ -d "$lib_dir" ]] || continue
  export LD_LIBRARY_PATH="$lib_dir:$LD_LIBRARY_PATH"
  [[ -e "$lib_dir/libcublas.so.12" ]] && found_cublas=1
done

if [[ "$found_cublas" -eq 0 ]]; then
  echo "libcublas.so.12 not found under .nix-gc-roots/*/lib" >&2
  echo "ASR will fall back to CPU (~8x slower). Restore the roots with:" >&2
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
