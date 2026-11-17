#!/usr/bin/env bash
set -euo pipefail

# An Actions artifact does not preserve executable permissions. Restore the
# permission for a CI-built binary and verify its native library dependencies.
# Discloud must NEVER compile llama.cpp from scratch during application deploy.
mkdir -p runtime/bin
if [ -s runtime/bin/llama-server ]; then
  chmod u+x runtime/bin/llama-server
  export LD_LIBRARY_PATH="$(pwd)/runtime/bin:${LD_LIBRARY_PATH:-}"
  if ! runtime/bin/llama-server --help >/dev/null 2>&1; then
    echo 'ERROR: Prebuilt llama-server cannot execute with the bundled runtime libraries.' >&2
    exit 1
  fi
  echo 'Validated prebuilt CPU llama-server for this host.'
  exit 0
fi

if [ "${RIVAL_BUILD_FROM_SOURCE:-}" != "1" ]; then
  echo 'ERROR: Missing prebuilt llama-server. Upload the DIRECT Discloud GitHub artifact ZIP, not the repo or source folder.' >&2
  exit 1
fi

# ONLY in GitHub Actions' isolated builder, never during Discloud deployment.
for required in git cmake c++; do
  if ! command -v "$required" >/dev/null; then
    echo "ERROR: Missing required build tool: $required" >&2
    exit 1
  fi
done

# b12480 includes the LFM3 JSON-grammar and reasoning-budget fixes in
# upstream #31374 and #31380 (first present in b12024). Pin the verified
# immutable release commit.
# Bump from b12024: b12024's ui-assets.cmake requires workbox[hash].js in the
# downloaded llama-ui archive, but the current archive no longer ships it
# (the UI moved to SvelteKit's service-worker.js), so the Discloud LLM ZIP
# build failed three times identically. b12480's ui-assets.cmake accepts
# either service-worker.js or workbox[hash].js, matching the current archive.
REF=b12480
EXPECTED_SHA=7f106b545965d601eb875ec8d2381288ee9b98f9
if [ ! -d runtime/llama.cpp ]; then
  git clone --depth 1 --branch "$REF" https://github.com/ggml-org/llama.cpp.git runtime/llama.cpp
fi
if [ "$(git -C runtime/llama.cpp rev-parse HEAD)" != "$EXPECTED_SHA" ]; then
  echo 'ERROR: pinned llama.cpp source does not match validated release commit.' >&2
  exit 1
fi
cmake -S runtime/llama.cpp -B runtime/llama.cpp/build \
  -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build runtime/llama.cpp/build --target llama-server --parallel 2
cp runtime/llama.cpp/build/bin/llama-server runtime/bin/llama-server
# Ship the actual library bytes at every SONAME, including .so.1 aliases.
# ZIP/artifact transports can strip symlinks; never rely on them surviving upload.
find runtime/llama.cpp/build/bin -maxdepth 1 \( -type f -o -type l \) \
  \( -name 'libggml*.so*' -o -name 'libllama*.so*' -o -name 'libmtmd*.so*' \) \
  -exec cp -L '{}' runtime/bin/ \;
chmod u+x runtime/bin/llama-server
export LD_LIBRARY_PATH="$(pwd)/runtime/bin:${LD_LIBRARY_PATH:-}"
runtime/bin/llama-server --help >/dev/null
printf '%s\n' 'CI-built llama-server validated.'
