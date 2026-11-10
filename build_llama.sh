#!/usr/bin/env bash
# Build llama-server for Pascal (sm_61) and pin its runtime against nix GC.
#
# The binary loads glibc and libstdc++ from the nix store, and shell.nix roots
# only the CUDA components. Without a root for the other two, the next
# nix-collect-garbage leaves a binary that fails with "cannot execute: required
# file not found". This script roots every store path the finished binary loads.
#
# A build directory configured before a garbage collection names compilers that
# are gone. Remove vendor/llama.cpp/build and run this again.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

LLAMA_SOURCE="${LLAMA_SOURCE:-$ROOT/vendor/llama.cpp}"

if [[ ! -f "$LLAMA_SOURCE/CMakeLists.txt" ]]; then
  echo "No llama.cpp checkout at $LLAMA_SOURCE" >&2
  echo "Clone the pinned commit first: see step 1 of 'One-time setup' in README.md" >&2
  exit 1
fi

nix-shell shell.nix --run "
  cmake -S '$LLAMA_SOURCE' -B '$LLAMA_SOURCE/build' \
        -DGGML_CUDA=ON -DGGML_CUDA_F16=ON -DGGML_CUDA_FORCE_MMQ=ON \
        -DCMAKE_CUDA_ARCHITECTURES=61 -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF &&
  cmake --build '$LLAMA_SOURCE/build' --config Release -j --target llama-server llama-bench
"

BINARY="$LLAMA_SOURCE/build/bin/llama-server"

# The real libcuda lives under /run/opengl-driver/lib, as in start_llm.sh.
loaded="$(LD_LIBRARY_PATH="/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ldd "$BINARY")"

if grep -q "not found" <<<"$loaded"; then
  echo "llama-server cannot resolve these libraries:" >&2
  grep "not found" <<<"$loaded" >&2
  exit 1
fi

mkdir -p "$ROOT/.nix-gc-roots"
grep -o '/nix/store/[a-z0-9]\{32\}-[^/]*' <<<"$loaded" | sort -u | while read -r store_path; do
  nix-store --add-root "$ROOT/.nix-gc-roots/$(basename "$store_path")" \
            --indirect --realise "$store_path" > /dev/null
  echo "rooted $store_path"
done
