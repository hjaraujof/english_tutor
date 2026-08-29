{ pkgs ? import <nixpkgs> {
    config.allowUnfree = true;
    config.cudaSupport = true;
} }:

let
  # Driver 535 caps the runtime at CUDA 12.2, and 12.8-built cubins fail to
  # load on it ("device kernel image is invalid"). 12.6 is the newest set in
  # nixpkgs whose sm_61 cubins still load on the 535 driver line — the same
  # compatibility guarantee torch cu126 wheels rely on (driver >= 525).
  cuda = pkgs.cudaPackages_12_6;

  # Request components, not `cuda.cudatoolkit`. That attribute is the merged
  # runfile package, and realising it drags in cuFFT, cuSOLVER, cuSPARSE and
  # NPP — none of which ggml-cuda references. The merge derivation needs more
  # scratch space than this 126 GB disk can spare, so it fails with
  # "No space left on device" and takes the whole shell down with it.
  #
  # ggml-cuda needs exactly these four: nvcc to compile, cudart to link
  # against, cuBLAS for the matmuls, and CCCL for the Thrust/CUB headers.
  cudaComponents = [
    cuda.cuda_nvcc
    cuda.cuda_cudart
    cuda.libcublas
    cuda.cuda_cccl
  ];
in
pkgs.mkShell {
  buildInputs = cudaComponents ++ [
    # Host compiler compatible with nvcc 12.6 — the channel's default gcc 14 is not.
    cuda.backendStdenv.cc
    pkgs.cmake
    pkgs.git
    pkgs.pkg-config
    pkgs.ffmpeg
  ];

  shellHook = ''
    export CUDA_PATH=${cuda.cuda_nvcc}
    export CUDAToolkit_ROOT=${cuda.cuda_nvcc}
    export CC=${cuda.backendStdenv.cc}/bin/cc
    export CXX=${cuda.backendStdenv.cc}/bin/c++
    export CUDAHOSTCXX=${cuda.backendStdenv.cc}/bin/c++
    export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath cudaComponents}:$LD_LIBRARY_PATH

    # Pin the CUDA closure against garbage collection and expose a stable path
    # for start_backend.sh, which needs libcublas.so.12 on LD_LIBRARY_PATH or
    # faster-whisper silently drops to CPU. Without a root, the next
    # nix-collect-garbage takes these away and the ASR quietly halves in speed.
    # Root the `lib` outputs specifically. Rooting a component's default output
    # protects the lib output through its closure, but that default output holds
    # only nix-support and LICENSE — so no path under .nix-gc-roots would
    # actually contain libcublas.so.12 for start_backend.sh to glob.
    mkdir -p .nix-gc-roots
    for component in ${pkgs.lib.concatStringsSep " " (map (p: toString (pkgs.lib.getLib p)) cudaComponents)}; do
      nix-store --add-root ".nix-gc-roots/$(basename "$component")" \
                --indirect --realise "$component" > /dev/null
    done

    echo "english_tutor build shell ready"
    echo "  cmake : $(cmake --version | head -1)"
    echo "  nvcc  : $(nvcc --version 2>/dev/null | tail -1 || echo 'nvcc not on PATH')"
  '';
}
