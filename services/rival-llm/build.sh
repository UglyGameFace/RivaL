#!/usr/bin/env bash
set -euo pipefail

# Build separately from RivaL's 512MB bot. Never include >1GB model weights
# in source control, Discloud build archives, or the RivaL app package.
if ! command -v git >/dev/null || ! command -v cmake >/dev/null; then
  echo 'ERROR: Install Discloud APT tools and libgl to provide git, cmake, make.' >&2
  exit 1
fi
if ! command -v c++ >/dev/null; then
  echo 'ERROR: No C++ compiler in this Discloud build image. Build binary in CI or on supported builder.' >&2
  exit 1
fi
mkdir -p runtime/bin
if [ -x runtime/bin/llama-server ]; then
  echo 'Existing llama-server binary present, skipping build.'
  exit 0
fi
REF=v0.8.0
if [ ! -d runtime/llama.cpp ]; then
  git clone --depth 1 --branch "${REF}" https://github.com/ggml-org/llama.cpp.git runtime/llama.cpp
fi
cmake -S runtime/llama.cpp -B runtime/llama.cpp/build \
  -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build runtime/llama.cpp/build --target llama-server --parallel 2
cp runtime/llama.cpp/build/bin/llama-server runtime/bin/llama-server
find runtime/llama.cpp/build -type f \( -name 'libggml*.so*' -o -name 'libllama*.so*' \) \
  -exec cp -P '{}' runtime/bin/ \;
chmod +x runtime/bin/llama-server
runtime/bin/llama-server --help >/dev/null
printf '%s\n' 'llama-server binary validated; official GGUF fetch begins at first start.'
