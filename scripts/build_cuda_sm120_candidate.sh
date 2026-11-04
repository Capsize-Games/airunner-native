#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUTS="$ROOT_DIR/runtime_sidecars/cuda-sm120-candidate.env"
SOURCE_DIR=""
CMAKE_ARCHIVE=""
OUTPUT_ROOT="$ROOT_DIR/build/runtime-sidecars-candidates/cuda-sm120"
WORK_ROOT="$ROOT_DIR/build/runtime-sidecars-candidates-work/cuda-sm120"
JOBS=2
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: scripts/build_cuda_sm120_candidate.sh --source DIR --cmake-archive FILE [options]

Builds a Linux llama-server candidate for CUDA SM 120 in an isolated, offline
container. It does not build Whisper, touch the v0.2.3 artifact path, or publish.

Options:
  --source DIR       Clean llama.cpp checkout at the locked commit
  --cmake-archive FILE  Hash-pinned Kitware CMake 3.31.6 archive
  --output-root DIR  Candidate output root
  --work-root DIR    Candidate build work root
  --jobs COUNT       Parallel build jobs, 1-8 (default: 2)
  --dry-run          Validate inputs and print the container invocation
EOF
}

die() { echo "error: $*" >&2; exit 1; }

paths_overlap() {
  local left="$1" right="$2"
  [[ "$left" == "$right" || "$left" == "$right"/* || "$right" == "$left"/* ]]
}

while (($#)); do
  case "$1" in
    --source) SOURCE_DIR="${2:?missing value for --source}"; shift 2 ;;
    --cmake-archive) CMAKE_ARCHIVE="${2:?missing value for --cmake-archive}"; shift 2 ;;
    --output-root) OUTPUT_ROOT="${2:?missing value for --output-root}"; shift 2 ;;
    --work-root) WORK_ROOT="${2:?missing value for --work-root}"; shift 2 ;;
    --jobs) JOBS="${2:?missing value for --jobs}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$JOBS" =~ ^[1-8]$ ]] || die "jobs must be 1-8"
[[ -n "$SOURCE_DIR" && -n "$CMAKE_ARCHIVE" ]] || { usage >&2; exit 2; }
[[ -d "$SOURCE_DIR/.git" ]] || die "source must be a git checkout"
[[ -f "$CMAKE_ARCHIVE" && ! -L "$CMAKE_ARCHIVE" ]] || die "CMake archive is missing or a symlink"

# Read a fixed, inert key/value lock. Never source it as shell code.
declare -A lock=()
while IFS='=' read -r key value; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  [[ "$key" =~ ^[A-Z0-9_]+$ && "$value" =~ ^[A-Za-z0-9._:/@-]+$ ]] || die "invalid lock entry"
  case "$key" in
    LLAMA_CPP_REPOSITORY|LLAMA_CPP_COMMIT|LLAMA_CPP_TREE|LLAMA_CPP_VERSION|LLAMA_CPP_PATCH_PATH|LLAMA_CPP_PATCH_SHA256|CUDA_IMAGE|CUDA_ARCHITECTURES|\
    CMAKE_VERSION|CMAKE_ARCHIVE_SHA256|CMAKE_BINARY_SHA256) ;;
    *) die "unknown lock key: $key" ;;
  esac
  [[ -z "${lock[$key]+x}" ]] || die "duplicate lock key: $key"
  lock[$key]="$value"
done < "$INPUTS"
for key in LLAMA_CPP_REPOSITORY LLAMA_CPP_COMMIT LLAMA_CPP_TREE LLAMA_CPP_VERSION LLAMA_CPP_PATCH_PATH LLAMA_CPP_PATCH_SHA256 CUDA_IMAGE \
           CUDA_ARCHITECTURES CMAKE_VERSION CMAKE_ARCHIVE_SHA256 CMAKE_BINARY_SHA256; do
  [[ -n "${lock[$key]:-}" ]] || die "missing lock key: $key"
done
[[ "${lock[LLAMA_CPP_COMMIT]}" == 7acb4e8cd2ce21f457d1298e75fad729520d263c ]] || die "unexpected source pin"
[[ "${lock[LLAMA_CPP_TREE]}" == 5a77024ea13397365f3be1610bee5573b0c42f1f ]] || die "unexpected source tree pin"
[[ "${lock[LLAMA_CPP_PATCH_PATH]}" == runtime_sidecars/patches/llama.cpp-cuda-all-mtp-offload.patch ]] || die "unexpected source patch path"
[[ "${lock[LLAMA_CPP_PATCH_SHA256]}" =~ ^[a-f0-9]{64}$ ]] || die "invalid source patch SHA-256 pin"
PATCH_FILE="$ROOT_DIR/${lock[LLAMA_CPP_PATCH_PATH]}"
[[ -f "$PATCH_FILE" && ! -L "$PATCH_FILE" ]] || die "reviewed CUDA source patch is missing or a symlink"
patch_hash="$(sha256sum "$PATCH_FILE" | cut -d ' ' -f1)"
[[ "$patch_hash" == "${lock[LLAMA_CPP_PATCH_SHA256]}" ]] || die "CUDA source patch SHA-256 does not match lock"
[[ "${lock[LLAMA_CPP_VERSION]}" =~ ^b([0-9]+)$ ]] || die "unexpected llama.cpp build label"
LOCK_LLAMA_BUILD_NUMBER="${BASH_REMATCH[1]}"
[[ "${lock[CUDA_IMAGE]}" == nvidia/cuda:12.9.2-devel-ubuntu24.04@sha256:16656a1ef115bca9e1f820c6349876f1486d2b3c9a0e615773799fe402960dc5 ]] || die "unexpected CUDA image"
[[ "${lock[CUDA_ARCHITECTURES]}" == 120a-real ]] || die "unexpected CUDA target"

source_real="$(realpath -e "$SOURCE_DIR")"
cmake_archive_real="$(realpath -e "$CMAKE_ARCHIVE")"
cmake_archive_hash="$(sha256sum "$cmake_archive_real" | cut -d ' ' -f1)"
[[ "$cmake_archive_hash" == "${lock[CMAKE_ARCHIVE_SHA256]}" ]] || die "CMake archive SHA-256 does not match lock"
[[ "$(git -C "$source_real" rev-parse HEAD)" == "${lock[LLAMA_CPP_COMMIT]}" ]] || die "source HEAD does not match reviewed commit"
source_tree="$(git -C "$source_real" rev-parse 'HEAD^{tree}')"
[[ "$source_tree" == "${lock[LLAMA_CPP_TREE]}" ]] || die "source tree does not match reviewed tree"
[[ -z "$(git -C "$source_real" status --porcelain --untracked-files=all)" ]] || die "source checkout is modified or has untracked files"
source_build_number="$(git -C "$source_real" rev-list --count HEAD)"
[[ "$source_build_number" == "$LOCK_LLAMA_BUILD_NUMBER" ]] || die "pinned build label does not match source commit count"

output_parent="$(realpath -m "$(dirname "$OUTPUT_ROOT")")"
output_candidate="$output_parent/$(basename "$OUTPUT_ROOT")"
work_candidate="$(realpath -m "$WORK_ROOT")"
released_root="$(realpath -m "$ROOT_DIR/build/runtime-sidecars")"
for reserved in "$released_root" "$source_real" "$cmake_archive_real" \
  "$(realpath -m "$ROOT_DIR/.git")" \
  "$(realpath -m "$ROOT_DIR/.github")" \
  "$(realpath -m "$ROOT_DIR/runtime_sidecars")" \
  "$(realpath -m "$ROOT_DIR/scripts")" \
  "$(realpath -m "$ROOT_DIR/review")" \
  "$(realpath -m "$ROOT_DIR/tests")" \
  "$(realpath -m "$ROOT_DIR/README.md")" \
  "$(realpath -m "$ROOT_DIR/LICENSE")"; do
  paths_overlap "$output_candidate" "$reserved" && die "candidate output overlaps reserved input/config path: $reserved"
  paths_overlap "$work_candidate" "$reserved" && die "candidate work path overlaps reserved input/config path: $reserved"
done
paths_overlap "$output_candidate" "$work_candidate" && die "candidate output and work paths overlap"
paths_overlap "$output_candidate" "$source_real" && die "candidate output overlaps source checkout"
paths_overlap "$work_candidate" "$source_real" && die "candidate work path overlaps source checkout"
paths_overlap "$output_candidate" "$cmake_archive_real" && die "candidate output overlaps CMake archive"
paths_overlap "$work_candidate" "$cmake_archive_real" && die "candidate work path overlaps CMake archive"
mkdir -p "$output_parent" "$(dirname "$WORK_ROOT")"
if [[ -e "$OUTPUT_ROOT" || -L "$OUTPUT_ROOT" ]]; then
  [[ -d "$OUTPUT_ROOT" && ! -L "$OUTPUT_ROOT" ]] || die "candidate output must be a real directory"
  [[ -f "$OUTPUT_ROOT/.airunner-native-candidate" && ! -L "$OUTPUT_ROOT/.airunner-native-candidate" && \
     "$(<"$OUTPUT_ROOT/.airunner-native-candidate")" == "airunner-native-candidate-v1:cuda-sm120" ]] || \
    die "refusing to reuse an unmarked candidate output directory"
  [[ -z "$(find "$OUTPUT_ROOT" -mindepth 1 ! -name .airunner-native-candidate -print -quit)" ]] || \
    die "candidate output is not fresh; select a new output root"
else
  mkdir -p "$OUTPUT_ROOT"
  printf 'airunner-native-candidate-v1:cuda-sm120\n' > "$OUTPUT_ROOT/.airunner-native-candidate"
fi
if [[ -e "$WORK_ROOT" || -L "$WORK_ROOT" ]]; then
  [[ -d "$WORK_ROOT" && ! -L "$WORK_ROOT" ]] || die "candidate work path must be a real directory"
  [[ -f "$WORK_ROOT/.airunner-native-candidate-work" && ! -L "$WORK_ROOT/.airunner-native-candidate-work" && \
     "$(<"$WORK_ROOT/.airunner-native-candidate-work")" == "airunner-native-candidate-work-v1:cuda-sm120" ]] || \
    die "refusing to reuse an unmarked candidate work directory"
  work_inputs="$WORK_ROOT/.candidate-work-inputs.env"
  if [[ -d "$WORK_ROOT/build" ]]; then
    [[ -f "$work_inputs" && ! -L "$work_inputs" ]] || die "existing work tree has no pinned input receipt"
    grep -Fxq "source_commit=${lock[LLAMA_CPP_COMMIT]}" "$work_inputs" || die "existing work source identity differs"
    grep -Fxq "source_tree=${lock[LLAMA_CPP_TREE]}" "$work_inputs" || die "existing work source tree differs"
    grep -Fxq "source_patch_sha256=${lock[LLAMA_CPP_PATCH_SHA256]}" "$work_inputs" || die "existing work source patch differs"
    grep -Fxq "source_build_number=$source_build_number" "$work_inputs" || die "existing work build number differs"
    grep -Fxq "cuda_image=${lock[CUDA_IMAGE]}" "$work_inputs" || die "existing work CUDA image differs"
    grep -Fxq "cuda_architectures=${lock[CUDA_ARCHITECTURES]}" "$work_inputs" || die "existing work CUDA architecture differs"
    grep -Fxq "cmake_archive_sha256=$cmake_archive_hash" "$work_inputs" || die "existing work CMake input differs"
  else
    [[ -z "$(find "$WORK_ROOT" -mindepth 1 ! -name .airunner-native-candidate-work -print -quit)" ]] || \
      die "candidate work directory is not fresh; select a new work root"
  fi
else
  mkdir -p "$WORK_ROOT"
  printf 'airunner-native-candidate-work-v1:cuda-sm120\n' > "$WORK_ROOT/.airunner-native-candidate-work"
fi
if [[ ! -f "$WORK_ROOT/.candidate-work-inputs.env" ]]; then
  cat > "$WORK_ROOT/.candidate-work-inputs.env" <<EOF
source_commit=${lock[LLAMA_CPP_COMMIT]}
source_tree=${lock[LLAMA_CPP_TREE]}
source_patch_sha256=${lock[LLAMA_CPP_PATCH_SHA256]}
source_build_number=$source_build_number
cuda_image=${lock[CUDA_IMAGE]}
cuda_architectures=${lock[CUDA_ARCHITECTURES]}
cmake_archive_sha256=$cmake_archive_hash
EOF
fi
OUTPUT_ROOT="$(realpath -e "$OUTPUT_ROOT")"
WORK_ROOT="$(realpath -e "$WORK_ROOT")"
build_dir="$WORK_ROOT/build"
patched_source="$WORK_ROOT/patched-source"
mkdir -p "$build_dir"
docker_bin="$(command -v docker)" || die "Docker is required"
cmake_stage=""
BUILD_SUCCEEDED=0
STATUS_FILE="$OUTPUT_ROOT/candidate-status.env"
CID_FILE="$OUTPUT_ROOT/build-container-id.txt"
on_exit() {
  local exit_code=$?
  local container_id=""
  local server_sha256=""
  if [[ -s "$CID_FILE" ]]; then
    container_id="$(head -n1 "$CID_FILE" | tr -d '\r\n')"
  fi
  if [[ "$exit_code" != 0 && "$container_id" =~ ^[0-9a-f]{64}$ ]]; then
    if [[ "$("$docker_bin" inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" == true ]]; then
      "$docker_bin" rm --force "$container_id" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -f "$OUTPUT_ROOT/llama-server" ]]; then
    server_sha256="$(sha256sum "$OUTPUT_ROOT/llama-server" | cut -d ' ' -f1)"
  fi
  python3 - "$OUTPUT_ROOT/build-container-receipt.json" "$container_id" "$exit_code" "$server_sha256" "${lock[CUDA_IMAGE]}" <<'PY'
import json
import sys
from pathlib import Path

receipt_path, container_id, exit_code, server_sha256, image = sys.argv[1:]
receipt = {
    "container_id": container_id,
    "image": image,
    "runtime": "runc",
    "network": "none",
    "gpu_device_requests": None,
    "cpu_limit": 2,
    "memory_limit": "8g",
    "command_constraints": ["--platform=linux/amd64", "--pull=never", "--runtime=runc", "--network=none", "--cpus=2", "--memory=8g", "--pids-limit=1024", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only"],
    "server_sha256": server_sha256,
    "result": f"docker run exit {exit_code}",
}
Path(receipt_path).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  if [[ "$BUILD_SUCCEEDED" != 1 ]]; then
    printf 'status=failed\nfinished_utc=%s\nexit_code=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$exit_code" >> "$STATUS_FILE"
  fi
  [[ -z "$cmake_stage" || ! -e "$cmake_stage" ]] || rm -rf -- "$cmake_stage"
}
if [[ "$DRY_RUN" != 1 ]]; then
  python3 "$ROOT_DIR/scripts/native_recipe_identity.py" capture \
    --profile cuda-sm120a --output "$OUTPUT_ROOT/producer-recipe.json"
  BUILD_STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "$STATUS_FILE" <<EOF
status=running
started_utc=$BUILD_STARTED_UTC
source_commit=${lock[LLAMA_CPP_COMMIT]}
source_tree=${lock[LLAMA_CPP_TREE]}
source_version=${lock[LLAMA_CPP_VERSION]}
source_build_number=$source_build_number
cmake_archive_sha256=$cmake_archive_hash
cuda_image=${lock[CUDA_IMAGE]}
cuda_architectures=${lock[CUDA_ARCHITECTURES]}
EOF
  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
else
  trap '[[ -z "$cmake_stage" || ! -e "$cmake_stage" ]] || rm -rf -- "$cmake_stage"' EXIT
fi
cmake_stage="$(mktemp -d "$WORK_ROOT/.cmake-stage.XXXXXX")"
tar --extract --gzip --no-same-owner --no-same-permissions --file "$cmake_archive_real" --directory "$cmake_stage"
cmake_real="$cmake_stage/cmake-${lock[CMAKE_VERSION]}-linux-x86_64"
[[ -x "$cmake_real/bin/cmake" ]] || die "pinned CMake archive has no expected executable"
cmake_hash="$(sha256sum "$cmake_real/bin/cmake" | cut -d ' ' -f1)"
[[ "$cmake_hash" == "${lock[CMAKE_BINARY_SHA256]}" ]] || die "CMake binary SHA-256 does not match lock"
[[ "$("$cmake_real/bin/cmake" --version | head -n1)" == "cmake version ${lock[CMAKE_VERSION]}" ]] || die "CMake version does not match lock"

cmd=("$docker_bin" run --rm --pull=never --cidfile "$CID_FILE" --runtime=runc --platform linux/amd64 --network=none
  --cpus=2 --memory=8g --pids-limit=1024 --cap-drop=ALL
  --security-opt=no-new-privileges --read-only
  --tmpfs /tmp:rw,noexec,nosuid,size=256m
  --user "$(id -u):$(id -g)" --env HOME=/tmp --env JOBS
  --env LOCK_LLAMA_CPP_COMMIT --env LOCK_LLAMA_CPP_TREE --env LOCK_LLAMA_CPP_VERSION --env LOCK_LLAMA_BUILD_NUMBER
  --env LOCK_LLAMA_CPP_PATCH_SHA256 --env LOCK_LLAMA_CPP_PATCH_PATH
  --env LOCK_CUDA_IMAGE --env LOCK_CUDA_ARCHITECTURES
  --env LOCK_CMAKE_VERSION --env LOCK_CMAKE_ARCHIVE_SHA256 --env LOCK_CMAKE_BINARY_SHA256
  --mount "type=bind,src=$patched_source,dst=/src,readonly"
  --mount "type=bind,src=$build_dir,dst=/build"
  --mount "type=bind,src=$cmake_real,dst=/opt/cmake,readonly"
  --mount "type=bind,src=$OUTPUT_ROOT,dst=/out"
  --entrypoint /bin/bash "${lock[CUDA_IMAGE]}" -ceu)
container_script='mkdir -p /tmp/cuda-stubs
ln -s /usr/local/cuda/targets/x86_64-linux/lib/stubs/libcuda.so /tmp/cuda-stubs/libcuda.so.1
export LIBRARY_PATH="/tmp/cuda-stubs:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="/tmp/cuda-stubs:${LD_LIBRARY_PATH:-}"
export PATH="/opt/cmake/bin:$PATH"
cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
  -DCMAKE_INSTALL_RPATH=\$ORIGIN/../lib -DCMAKE_BUILD_RPATH=\$ORIGIN/../lib \
  -DGGML_NATIVE=OFF -DGGML_CUDA=ON -DGGML_CUDA_ARCHITECTURES=120a-real \
  -DCMAKE_CUDA_ARCHITECTURES=120a-real -DGGML_CUDA_CUB_3DOT2=OFF \
  -DGGML_CUDA_NCCL=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_NUMBER="$LOCK_LLAMA_BUILD_NUMBER" -DLLAMA_BUILD_COMMIT="$LOCK_LLAMA_CPP_COMMIT" \
  -DGGML_BUILD_COMMIT="$LOCK_LLAMA_CPP_COMMIT" \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_WEBUI=OFF \
  -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_APP=OFF -DGGML_RPC=OFF \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/tmp/cuda-stubs
grep -E "^GGML_CUDA:.*=ON$" /build/CMakeCache.txt
grep -E "^GGML_CUDA_ARCHITECTURES:[^=]*=120a-real$" /build/CMakeCache.txt
grep -E "^CMAKE_CUDA_ARCHITECTURES:[^=]*=120a-real$" /build/CMakeCache.txt
grep -E "^LLAMA_BUILD_NUMBER:[^=]*=${LOCK_LLAMA_BUILD_NUMBER}$" /build/CMakeCache.txt
grep -E "^LLAMA_BUILD_COMMIT:[^=]*=${LOCK_LLAMA_CPP_COMMIT}$" /build/CMakeCache.txt
grep -E "^GGML_BUILD_COMMIT:[^=]*=${LOCK_LLAMA_CPP_COMMIT}$" /build/CMakeCache.txt
cmake --build /build --config Release --target llama-server --parallel "$JOBS"
test -x /build/bin/llama-server
version="$(/build/bin/llama-server --version 2>&1)"
printf "%s\n" "$version" | grep -F "${LOCK_LLAMA_CPP_COMMIT}" >/dev/null
/build/bin/llama-server --help >/dev/null
cp /build/bin/llama-server /out/llama-server
cp /src/LICENSE /out/LICENSE-llama.cpp.txt
cp /build/ggml/src/ggml-cuda/CMakeFiles/ggml-cuda.dir/flags.make /out/cuda-compile-flags.make
cuobjdump --list-elf /build/ggml/src/ggml-cuda/libggml-cuda.a > /out/elf-cuda-inspection.txt
cubins="$(grep -oE "sm_[0-9]+[a-z]?\.cubin" /out/elf-cuda-inspection.txt || true)"
test -n "$cubins"
test "$(printf "%s\n" "$cubins" | sort -u)" = "sm_120a.cubin"
cubin_count="$(printf "%s\n" "$cubins" | wc -l | tr -d " ")"
test "$cubin_count" = 138
printf "source_commit=%s\nsource_tree=%s\nsource_version=%s\nCUDA enabled=ON\ncuda_architectures=%s\ncuda_toolkit=%s\ncuda_archive_sha256=%s\ncubin_target=sm_120a\ncubin_count=%s\n" \
  "$LOCK_LLAMA_CPP_COMMIT" "$LOCK_LLAMA_CPP_TREE" "$LOCK_LLAMA_CPP_VERSION" "$LOCK_CUDA_ARCHITECTURES" \
  "$(nvcc --version | sed -n "s/.*release \([^,]*\),.*/\1/p" | tail -n1)" \
  "$(sha256sum /build/ggml/src/ggml-cuda/libggml-cuda.a | cut -d " " -f1)" "$cubin_count" \
  > /out/cuda-build-config.txt
sha256sum /out/llama-server > /out/llama-server.sha256
printf "source_commit=%s\nsource_tree=%s\nsource_version=%s\nsource_build_number=%s\nsource_patch_sha256=%s\nsource_patch_path=%s\ncuda_image=%s\ncuda_architectures=%s\ncmake_version=%s\ncmake_archive_sha256=%s\ncmake_binary_sha256=%s\n" \
  "$LOCK_LLAMA_CPP_COMMIT" "$LOCK_LLAMA_CPP_TREE" "$LOCK_LLAMA_CPP_VERSION" "$LOCK_LLAMA_BUILD_NUMBER" \
  "$LOCK_LLAMA_CPP_PATCH_SHA256" "$LOCK_LLAMA_CPP_PATCH_PATH" "$LOCK_CUDA_IMAGE" "$LOCK_CUDA_ARCHITECTURES" "$LOCK_CMAKE_VERSION" "$LOCK_CMAKE_ARCHIVE_SHA256" \
  "$LOCK_CMAKE_BINARY_SHA256" > /out/candidate-provenance.env
printf "%s\n" "$version" >> /out/candidate-provenance.env'

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'LOCK_LLAMA_CPP_COMMIT=%q LOCK_LLAMA_CPP_TREE=%q LOCK_LLAMA_CPP_VERSION=%q LOCK_LLAMA_BUILD_NUMBER=%q LOCK_LLAMA_CPP_PATCH_SHA256=%q LOCK_LLAMA_CPP_PATCH_PATH=%q LOCK_CUDA_IMAGE=%q LOCK_CUDA_ARCHITECTURES=%q LOCK_CMAKE_VERSION=%q LOCK_CMAKE_ARCHIVE_SHA256=%q LOCK_CMAKE_BINARY_SHA256=%q JOBS=%q ' \
    "${lock[LLAMA_CPP_COMMIT]}" "${lock[LLAMA_CPP_TREE]}" "${lock[LLAMA_CPP_VERSION]}" "$LOCK_LLAMA_BUILD_NUMBER" \
    "${lock[LLAMA_CPP_PATCH_SHA256]}" "${lock[LLAMA_CPP_PATCH_PATH]}" "${lock[CUDA_IMAGE]}" \
    "${lock[CUDA_ARCHITECTURES]}" "${lock[CMAKE_VERSION]}" "${lock[CMAKE_ARCHIVE_SHA256]}" "${lock[CMAKE_BINARY_SHA256]}" "$JOBS"
  printf '%q ' "${cmd[@]}" "$container_script"
  printf '\n'
  exit 0
fi

# Materialize a pristine archive at the locked commit, then apply only the hash-pinned
# patch. The supplied checkout remains clean and its commit/tree identify upstream.
if [[ -e "$patched_source" || -L "$patched_source" ]]; then
  [[ -d "$patched_source" && ! -L "$patched_source" && \
     -f "$patched_source/.airunner-patched-source-inputs.env" && ! -L "$patched_source/.airunner-patched-source-inputs.env" ]] || \
    die "refusing to reuse unmarked patched source"
  grep -Fxq "source_commit=${lock[LLAMA_CPP_COMMIT]}" "$patched_source/.airunner-patched-source-inputs.env" || die "patched source commit differs"
  grep -Fxq "source_tree=${lock[LLAMA_CPP_TREE]}" "$patched_source/.airunner-patched-source-inputs.env" || die "patched source tree differs"
  grep -Fxq "patch_sha256=${lock[LLAMA_CPP_PATCH_SHA256]}" "$patched_source/.airunner-patched-source-inputs.env" || die "patched source patch differs"
  rm -rf -- "$patched_source"
fi
mkdir -m 0755 "$patched_source"
git -C "$source_real" archive --format=tar "${lock[LLAMA_CPP_COMMIT]}" | tar --extract --no-same-owner --no-same-permissions --file=- --directory "$patched_source"
patch --dry-run --batch --forward --fuzz=0 -p1 --directory "$patched_source" < "$PATCH_FILE" >/dev/null || die "reviewed CUDA source patch does not apply cleanly"
patch --batch --forward --fuzz=0 -p1 --directory "$patched_source" < "$PATCH_FILE" >/dev/null || die "failed to apply reviewed CUDA source patch"
printf 'source_commit=%s\nsource_tree=%s\npatch_path=%s\npatch_sha256=%s\n' \
  "${lock[LLAMA_CPP_COMMIT]}" "${lock[LLAMA_CPP_TREE]}" "${lock[LLAMA_CPP_PATCH_PATH]}" "${lock[LLAMA_CPP_PATCH_SHA256]}" \
  > "$patched_source/.airunner-patched-source-inputs.env"

LOCK_LLAMA_CPP_COMMIT="${lock[LLAMA_CPP_COMMIT]}" \
LOCK_LLAMA_CPP_TREE="${lock[LLAMA_CPP_TREE]}" \
LOCK_LLAMA_CPP_VERSION="${lock[LLAMA_CPP_VERSION]}" \
LOCK_LLAMA_BUILD_NUMBER="$LOCK_LLAMA_BUILD_NUMBER" \
LOCK_LLAMA_CPP_PATCH_SHA256="${lock[LLAMA_CPP_PATCH_SHA256]}" \
LOCK_LLAMA_CPP_PATCH_PATH="${lock[LLAMA_CPP_PATCH_PATH]}" \
LOCK_CUDA_IMAGE="${lock[CUDA_IMAGE]}" \
LOCK_CUDA_ARCHITECTURES="${lock[CUDA_ARCHITECTURES]}" \
LOCK_CMAKE_VERSION="${lock[CMAKE_VERSION]}" \
LOCK_CMAKE_ARCHIVE_SHA256="${lock[CMAKE_ARCHIVE_SHA256]}" \
LOCK_CMAKE_BINARY_SHA256="${lock[CMAKE_BINARY_SHA256]}" \
JOBS="$JOBS" \
  "${cmd[@]}" "$container_script" 2>&1 | tee "$OUTPUT_ROOT/build.log"
python3 "$ROOT_DIR/scripts/native_recipe_identity.py" verify \
  --profile cuda-sm120a --output "$OUTPUT_ROOT/producer-recipe.json"
[[ "$(git -C "$source_real" rev-parse HEAD)" == "${lock[LLAMA_CPP_COMMIT]}" ]] || die "source changed during build"
[[ -z "$(git -C "$source_real" status --porcelain --untracked-files=all)" ]] || die "source changed during build"
BUILD_SUCCEEDED=1
printf 'status=success\nfinished_utc=%s\nserver_sha256=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$(sha256sum "$OUTPUT_ROOT/llama-server" | cut -d ' ' -f1)" >> "$STATUS_FILE"
