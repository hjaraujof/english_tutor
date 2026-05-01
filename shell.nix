{ pkgs ? import <nixpkgs> {
    config.allowUnfree = true;
    config.cudaSupport = true;
} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    cudatoolkit
    cudaPackages.cuda_cudart
    gcc
    cmake
    git
    pkg-config
    ffmpeg
  ];

  shellHook = ''
    export CUDA_PATH=${pkgs.cudatoolkit}
    export LD_LIBRARY_PATH=${pkgs.cudatoolkit}/lib:${pkgs.cudaPackages.cuda_cudart}/lib:$LD_LIBRARY_PATH
    echo "english_tutor build shell ready"
    echo "  cmake : $(cmake --version | head -1)"
    echo "  nvcc  : $(nvcc --version 2>/dev/null | tail -1 || echo 'nvcc not on PATH')"
  '';
}
