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
in
pkgs.mkShell {
  buildInputs = [
    cuda.cudatoolkit
    cuda.cuda_cudart
    # Host compiler compatible with nvcc 12.6 — the channel's default gcc 14 is not.
    cuda.backendStdenv.cc
    pkgs.cmake
    pkgs.git
    pkgs.pkg-config
    pkgs.ffmpeg
  ];

  shellHook = ''
    export CUDA_PATH=${cuda.cudatoolkit}
    export CC=${cuda.backendStdenv.cc}/bin/cc
    export CXX=${cuda.backendStdenv.cc}/bin/c++
    export CUDAHOSTCXX=${cuda.backendStdenv.cc}/bin/c++
    export LD_LIBRARY_PATH=${cuda.cudatoolkit}/lib:${cuda.cuda_cudart}/lib:$LD_LIBRARY_PATH
    echo "english_tutor build shell ready"
    echo "  cmake : $(cmake --version | head -1)"
    echo "  nvcc  : $(nvcc --version 2>/dev/null | tail -1 || echo 'nvcc not on PATH')"
  '';
}
