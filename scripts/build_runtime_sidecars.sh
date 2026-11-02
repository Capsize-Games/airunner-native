#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINS_FILE="$ROOT_DIR/runtime_sidecars/runtime_pins.env"

TARGET_PLATFORM="linux"
OUTPUT_ROOT="$ROOT_DIR/build/runtime-sidecars"
WORK_ROOT="$ROOT_DIR/build/runtime-sidecars-work"
CMAKE_BUILD_TYPE="Release"
JOBS="${AIRUNNER_BUILD_JOBS:-4}"
CLEAN=0
DRY_RUN=0
OFFLINE=0
ENABLE_CUDA="${AIRUNNER_SIDECAR_ENABLE_CUDA:-0}"
CUDA_ARCHITECTURES="${AIRUNNER_SIDECAR_CUDA_ARCHITECTURES:-}"

CMAKE_PLATFORM_ARGS=()
CMAKE_CUDA_ARGS=()
BINARY_SUFFIX=""

usage() {
    cat <<'EOF'
Usage: ./scripts/build_runtime_sidecars.sh [options]

Options:
  --target-platform linux|windows  Build native or Windows sidecars
  --output-root PATH               Bundle output root directory
  --work-root PATH                 Source and build work directory
  --jobs COUNT                     Build with 1-64 parallel jobs (default: 4)
  --clean                          Remove previous output for the target
  --dry-run                        Print build steps without running them
  --offline                        Use only existing, clean pinned source checkouts
  --enable-cuda                    Build llama.cpp and whisper.cpp with CUDA
  --cuda-architectures LIST        Set CUDA architectures for both builds
  -h, --help                       Show this help text
EOF
}

run_cmd() {
    echo "+ $*"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target-platform)
                TARGET_PLATFORM="$2"
                shift 2
                ;;
            --output-root)
                OUTPUT_ROOT="$2"
                shift 2
                ;;
            --work-root)
                WORK_ROOT="$2"
                shift 2
                ;;
            --jobs)
                JOBS="$2"
                shift 2
                ;;
            --clean)
                CLEAN=1
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --offline)
                OFFLINE=1
                shift
                ;;
            --enable-cuda)
                ENABLE_CUDA=1
                shift
                ;;
            --cuda-architectures)
                CUDA_ARCHITECTURES="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "Unknown option: $1" >&2
                usage >&2
                exit 1
                ;;
        esac
    done
}

load_pins() {
    local line key value
    local -A parsed=()

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ ! "$line" =~ ^([A-Z_]+)=([A-Za-z0-9._:/-]+)[[:space:]]*$ ]]; then
            echo "Invalid pin syntax in $PINS_FILE" >&2
            return 1
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        case "$key" in
            LLAMA_CPP_REPOSITORY|LLAMA_CPP_COMMIT|LLAMA_CPP_VERSION|\
            WHISPER_CPP_REPOSITORY|WHISPER_CPP_COMMIT|WHISPER_CPP_VERSION) ;;
            *) echo "Unknown pin field in $PINS_FILE: $key" >&2; return 1 ;;
        esac
        if [[ -v "parsed[$key]" ]]; then
            echo "Duplicate pin field in $PINS_FILE: $key" >&2
            return 1
        fi
        parsed["$key"]="$value"
    done < "$PINS_FILE"

    local field
    for field in LLAMA_CPP_REPOSITORY LLAMA_CPP_COMMIT LLAMA_CPP_VERSION \
        WHISPER_CPP_REPOSITORY WHISPER_CPP_COMMIT WHISPER_CPP_VERSION; do
        if [[ ! -v "parsed[$field]" ]]; then
            echo "Missing pin field in $PINS_FILE: $field" >&2
            return 1
        fi
    done

    [[ "${parsed[LLAMA_CPP_REPOSITORY]}" == "https://github.com/ggerganov/llama.cpp.git" ]] || {
        echo "Unexpected llama.cpp repository in $PINS_FILE" >&2; return 1;
    }
    [[ "${parsed[WHISPER_CPP_REPOSITORY]}" == "https://github.com/ggerganov/whisper.cpp.git" ]] || {
        echo "Unexpected whisper.cpp repository in $PINS_FILE" >&2; return 1;
    }
    for field in LLAMA_CPP_COMMIT WHISPER_CPP_COMMIT; do
        if [[ ! "${parsed[$field]}" =~ ^[0-9a-f]{40}$ ]]; then
            echo "Pin must be a full lowercase 40-character commit: $field" >&2
            return 1
        fi
    done
    [[ "${parsed[LLAMA_CPP_VERSION]}" =~ ^b[0-9]+$ ]] || {
        echo "Invalid llama.cpp version label in $PINS_FILE" >&2; return 1;
    }
    [[ "${parsed[WHISPER_CPP_VERSION]}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        echo "Invalid whisper.cpp version label in $PINS_FILE" >&2; return 1;
    }

    LLAMA_CPP_REPOSITORY="${parsed[LLAMA_CPP_REPOSITORY]}"
    LLAMA_CPP_COMMIT="${parsed[LLAMA_CPP_COMMIT]}"
    LLAMA_CPP_VERSION="${parsed[LLAMA_CPP_VERSION]}"
    WHISPER_CPP_REPOSITORY="${parsed[WHISPER_CPP_REPOSITORY]}"
    WHISPER_CPP_COMMIT="${parsed[WHISPER_CPP_COMMIT]}"
    WHISPER_CPP_VERSION="${parsed[WHISPER_CPP_VERSION]}"
}

detect_jobs() {
    if [[ ! "$JOBS" =~ ^[1-9][0-9]?$ ]] || (( JOBS > 64 )); then
        echo "Build job count must be an integer between 1 and 64" >&2
        return 1
    fi
    echo "$JOBS"
}

canonical_root() {
    local input="$1"
    realpath -m -- "$input"
}

assert_owned_target() {
    local root="$1" platform="$2" target marker canonical_root_path
    canonical_root_path="$(canonical_root "$root")"
    target="$canonical_root_path/$platform"
    marker="$target/.airunner-native-owned"
    if [[ -L "$target" ]]; then
        echo "Refusing to use symlink target: $target" >&2
        return 1
    fi
    if [[ -e "$target" ]]; then
        if [[ ! -d "$target" || -L "$marker" || ! -f "$marker" ]] || \
            [[ "$(<"$marker")" != "airunner-native-output-v1:$platform" ]]; then
            echo "Refusing to overwrite unowned target directory: $target" >&2
            return 1
        fi
    fi
}

configure_platform() {
    CMAKE_PLATFORM_ARGS=()
    CMAKE_CUDA_ARGS=()
    BINARY_SUFFIX=""
    C_COMPILER="${CC:-cc}"
    CXX_COMPILER="${CXX:-c++}"

    if [[ "$ENABLE_CUDA" == "1" ]]; then
        CMAKE_CUDA_ARGS=(-DGGML_CUDA=ON)
        if [[ -n "$CUDA_ARCHITECTURES" ]]; then
            CMAKE_CUDA_ARGS+=(
                -DGGML_CUDA_ARCHITECTURES="$CUDA_ARCHITECTURES"
                -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHITECTURES"
            )
        fi
    fi

    if [[ "$TARGET_PLATFORM" == "linux" ]]; then
        return
    fi

    if [[ "$TARGET_PLATFORM" != "windows" ]]; then
        echo "Unsupported target platform: $TARGET_PLATFORM" >&2
        exit 1
    fi

    local cxx
    local cc
    cxx="$(command -v x86_64-w64-mingw32-g++-posix || true)"
    if [[ -z "$cxx" ]]; then
        cxx="$(command -v x86_64-w64-mingw32-g++ || true)"
    fi
    cc="$(command -v x86_64-w64-mingw32-gcc-posix || true)"
    if [[ -z "$cc" ]]; then
        cc="$(command -v x86_64-w64-mingw32-gcc || true)"
    fi
    if [[ -z "$cxx" || -z "$cc" ]]; then
        echo "MinGW-w64 is required for Windows sidecar builds" >&2
        exit 1
    fi
    CXX_COMPILER="$cxx"
    C_COMPILER="$cc"

    CMAKE_PLATFORM_ARGS=(
        -DCMAKE_SYSTEM_NAME=Windows
        -DCMAKE_C_COMPILER="$cc"
        -DCMAKE_CXX_COMPILER="$cxx"
        -DCMAKE_EXE_LINKER_FLAGS=-static
    )
    BINARY_SUFFIX=".exe"

    local compat_header="$ROOT_DIR/runtime_sidecars/mingw_thread_power_throttling_compat.h"
    if printf '%s\n' \
        '#include <windows.h>' \
        '#ifndef THREAD_POWER_THROTTLING_CURRENT_VERSION' \
        '#error missing_thread_power_throttling_api' \
        '#endif' | "$cc" -E -x c - -o /dev/null 2>/dev/null; then
        # Toolchain headers declare the thread power-throttling API; nothing to do.
        :
    else
        echo "MinGW headers lack THREAD_POWER_THROTTLING_*; force-including compat header"
        CMAKE_PLATFORM_ARGS+=(
            -DCMAKE_C_FLAGS="-include $compat_header"
            -DCMAKE_CXX_FLAGS="-include $compat_header"
        )
    fi
}

clean_target() {
    if [[ "$CLEAN" != "1" ]]; then
        return
    fi
    assert_owned_target "$OUTPUT_ROOT" "$TARGET_PLATFORM"
    assert_owned_target "$WORK_ROOT" "$TARGET_PLATFORM"
    local output_target work_target output_root work_root
    output_root="$(canonical_root "$OUTPUT_ROOT")"
    work_root="$(canonical_root "$WORK_ROOT")"
    output_target="$output_root/$TARGET_PLATFORM"
    work_target="$work_root/$TARGET_PLATFORM"
    [[ ! -e "$output_target" ]] || run_cmd rm -rf -- "$output_target"
    [[ ! -e "$work_target" ]] || run_cmd rm -rf -- "$work_target"
}

prepare_layout() {
    assert_owned_target "$OUTPUT_ROOT" "$TARGET_PLATFORM"
    assert_owned_target "$WORK_ROOT" "$TARGET_PLATFORM"
    OUTPUT_ROOT="$(canonical_root "$OUTPUT_ROOT")"
    WORK_ROOT="$(canonical_root "$WORK_ROOT")"
    BUNDLE_ROOT="$OUTPUT_ROOT/$TARGET_PLATFORM"
    BUNDLE_BIN_DIR="$BUNDLE_ROOT/bin"
    BUNDLE_SHARE_DIR="$BUNDLE_ROOT/share/airunner"
    SOURCE_DIR="$WORK_ROOT/$TARGET_PLATFORM/src"
    BUILD_DIR="$WORK_ROOT/$TARGET_PLATFORM/build"

    run_cmd mkdir -p \
        "$BUNDLE_BIN_DIR" \
        "$BUNDLE_SHARE_DIR" \
        "$SOURCE_DIR" \
        "$BUILD_DIR"
    if [[ "$DRY_RUN" != "1" ]]; then
        printf 'airunner-native-output-v1:%s\n' "$TARGET_PLATFORM" > "$BUNDLE_ROOT/.airunner-native-owned"
        printf 'airunner-native-output-v1:%s\n' "$TARGET_PLATFORM" > "$WORK_ROOT/$TARGET_PLATFORM/.airunner-native-owned"
    fi
}

fetch_repo() {
    local name="$1"
    local repository="$2"
    local commit="$3"
    local destination="$SOURCE_DIR/$name"

    if [[ -L "$destination" ]]; then
        echo "Refusing symlink source checkout: $destination" >&2
        return 1
    fi
    if [[ "$OFFLINE" == "1" ]]; then
        if [[ ! -d "$destination/.git" ]]; then
            echo "Offline mode requires an existing Git checkout: $destination" >&2
            return 1
        fi
    elif [[ ! -d "$destination/.git" ]]; then
        run_cmd git clone --filter=blob:none "$repository" "$destination"
    fi

    if [[ "$DRY_RUN" != "1" ]]; then
        local origin status
        origin="$(git -C "$destination" remote get-url origin)" || return 1
        if [[ "$origin" != "$repository" ]]; then
            echo "Unexpected source origin for $name: $origin" >&2
            return 1
        fi
        status="$(git -C "$destination" status --porcelain --ignored --untracked-files=all)" || return 1
        if [[ -n "$status" ]]; then
            echo "Source checkout is not clean before pin verification: $destination" >&2
            return 1
        fi
    fi

    if [[ "$OFFLINE" != "1" ]]; then
        run_cmd git -C "$destination" fetch --depth 1 origin "$commit"
        run_cmd git -C "$destination" checkout --detach "$commit"
    fi

    if [[ "$DRY_RUN" != "1" ]]; then
        local head status
        head="$(git -C "$destination" rev-parse HEAD)" || return 1
        if [[ "$head" != "$commit" ]]; then
            echo "Source checkout does not match pinned commit for $name: $head" >&2
            return 1
        fi
        status="$(git -C "$destination" status --porcelain --ignored --untracked-files=all)" || return 1
        if [[ -n "$status" ]]; then
            echo "Source checkout is not clean after pin verification: $destination" >&2
            return 1
        fi
    fi
}

configure_build() {
    local source_path="$1"
    local build_path="$2"
    shift 2

    run_cmd cmake \
        -S "$source_path" \
        -B "$build_path" \
        -DCMAKE_BUILD_TYPE="$CMAKE_BUILD_TYPE" \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_RPC=OFF \
        -DGGML_CUDA_CUB_3DOT2=OFF \
        "${CMAKE_PLATFORM_ARGS[@]}" \
        "${CMAKE_CUDA_ARGS[@]}" \
        "$@"
}

build_target() {
    local build_path="$1"
    local target_name="$2"
    local jobs
    jobs="$(detect_jobs)"

    run_cmd cmake \
        --build "$build_path" \
        --config "$CMAKE_BUILD_TYPE" \
        --target "$target_name" \
        --parallel "$jobs"
}

copy_binary() {
    local build_path="$1"
    local binary_name="$2"
    local source_binary="$build_path/bin/${binary_name}${BINARY_SUFFIX}"
    local destination_binary="$BUNDLE_BIN_DIR/${binary_name}${BINARY_SUFFIX}"

    if [[ "$DRY_RUN" != "1" && ! -f "$source_binary" ]]; then
        echo "Expected binary was not produced: $source_binary" >&2
        exit 1
    fi

    run_cmd cp "$source_binary" "$destination_binary"
}

copy_upstream_licenses() {
    local upstream source destination
    for upstream in llama.cpp whisper.cpp; do
        source="$SOURCE_DIR/$upstream/LICENSE"
        destination="$BUNDLE_SHARE_DIR/licenses/$upstream-LICENSE"
        if [[ "$DRY_RUN" == "1" ]]; then
            echo "+ copy $source $destination"
            continue
        fi
        if [[ ! -f "$source" || -L "$source" ]]; then
            echo "Expected regular upstream license file was not found: $source" >&2
            return 1
        fi
        if [[ -L "${destination%/*}" ]]; then
            echo "Refusing symlink license output directory: ${destination%/*}" >&2
            return 1
        fi
        mkdir -p "${destination%/*}"
        cp -- "$source" "$destination"
    done
}

copy_windows_runtime_licenses() {
    [[ "$TARGET_PLATFORM" == "windows" ]] || return 0

    local source destination license
    if [[ "$DRY_RUN" != "1" ]]; then
        verify_windows_runtime_package_versions
    fi
    for license in \
        GPL-3.txt \
        mingw-gcc-runtime-copyright.txt \
        mingw-runtime-source.txt \
        winpthreads-COPYING.txt; do
        source="$ROOT_DIR/runtime_sidecars/licenses/$license"
        destination="$BUNDLE_SHARE_DIR/licenses/$license"
        if [[ "$DRY_RUN" == "1" ]]; then
            echo "+ copy $source $destination"
            continue
        fi
        if [[ ! -f "$source" || -L "$source" ]]; then
            echo "Expected regular MinGW runtime notice was not found: $source" >&2
            return 1
        fi
        if [[ -L "${destination%/*}" ]]; then
            echo "Refusing symlink license output directory: ${destination%/*}" >&2
            return 1
        fi
        mkdir -p "${destination%/*}"
        cp -- "$source" "$destination"
    done
}

verify_windows_runtime_package_versions() {
    local package expected actual
    local -a packages=(
        "gcc-mingw-w64-x86-64-posix-runtime=13.2.0-6ubuntu1+26.1"
        "gcc-mingw-w64-x86-64-posix=13.2.0-6ubuntu1+26.1"
        "g++-mingw-w64-x86-64-posix=13.2.0-6ubuntu1+26.1"
        "mingw-w64-x86-64-dev=11.0.1-3build1"
        "mingw-w64-common=11.0.1-3build1"
    )
    if ! command -v dpkg-query >/dev/null 2>&1; then
        echo "dpkg-query is required to verify the reviewed Windows runtime packages" >&2
        return 1
    fi
    for entry in "${packages[@]}"; do
        package="${entry%%=*}"
        expected="${entry#*=}"
        actual="$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)"
        if [[ "$actual" != "$expected" ]]; then
            echo "Windows runtime notice input mismatch for $package: expected $expected, got ${actual:-not-installed}" >&2
            return 1
        fi
    done
}

remove_whisper_generated_source_file() {
    local generated="$SOURCE_DIR/whisper.cpp/bindings/javascript/package.json"
    if [[ -e "$generated" || -L "$generated" ]]; then
        if [[ -L "$generated" || ! -f "$generated" ]]; then
            echo "Refusing to remove unexpected Whisper generated path: $generated" >&2
            return 1
        fi
        rm -- "$generated"
    fi
}

write_manifest() {
    local manifest_path="$BUNDLE_SHARE_DIR/runtime_manifest.env"
    local pins_path="$BUNDLE_SHARE_DIR/runtime_pins.env"

    if [[ "$DRY_RUN" == "1" ]]; then
        echo "+ write $manifest_path"
        echo "+ copy $PINS_FILE $pins_path"
        return
    fi

    local jobs cmake_version git_version cc_version cxx_version ninja_version
    local llama_hash whisper_hash cuda_arch
    jobs="$(detect_jobs)"
    cmake_version="$(version_line cmake --version)"
    git_version="$(version_line git --version)"
    cc_version="$(version_line "$C_COMPILER" --version)"
    cxx_version="$(version_line "$CXX_COMPILER" --version)"
    ninja_version="not-installed"
    if command -v ninja >/dev/null 2>&1; then
        ninja_version="$(version_line ninja --version)"
    fi
    cuda_arch="$CUDA_ARCHITECTURES"
    [[ -n "$cuda_arch" ]] || cuda_arch="compiler-default"
    llama_hash="$(sha256_file "$BUNDLE_BIN_DIR/llama-server${BINARY_SUFFIX}")"
    whisper_hash="$(sha256_file "$BUNDLE_BIN_DIR/whisper-server${BINARY_SUFFIX}")"

    cat > "$manifest_path" <<EOF
# Generated by scripts/build_runtime_sidecars.sh.
AIRUNNER_BUNDLE_ROOT=../..
AIRUNNER_PYTHON=../../python/bin/python
AIRUNNER_PYTHONPATH=../../app/site-packages
AIRUNNER_ENTRYPOINT=airunner.launcher
AIRUNNER_LLAMA_SERVER_BIN=../../bin/llama-server${BINARY_SUFFIX}
AIRUNNER_WHISPER_SERVER_BIN=../../bin/whisper-server${BINARY_SUFFIX}
EOF

    {
        manifest_set AIRUNNER_LLAMA_CPP_REPOSITORY "$LLAMA_CPP_REPOSITORY"
        manifest_set AIRUNNER_LLAMA_CPP_COMMIT "$LLAMA_CPP_COMMIT"
        manifest_set AIRUNNER_LLAMA_CPP_VERSION "$LLAMA_CPP_VERSION"
        manifest_set AIRUNNER_WHISPER_CPP_REPOSITORY "$WHISPER_CPP_REPOSITORY"
        manifest_set AIRUNNER_WHISPER_CPP_COMMIT "$WHISPER_CPP_COMMIT"
        manifest_set AIRUNNER_WHISPER_CPP_VERSION "$WHISPER_CPP_VERSION"
        manifest_set AIRUNNER_TARGET_PLATFORM "$TARGET_PLATFORM"
        manifest_set AIRUNNER_BUILD_TYPE "$CMAKE_BUILD_TYPE"
        manifest_set AIRUNNER_BUILD_JOBS "$jobs"
        manifest_set AIRUNNER_CMAKE_VERSION "$cmake_version"
        manifest_set AIRUNNER_GIT_VERSION "$git_version"
        manifest_set AIRUNNER_C_COMPILER_VERSION "$cc_version"
        manifest_set AIRUNNER_CXX_COMPILER_VERSION "$cxx_version"
        manifest_set AIRUNNER_NINJA_VERSION "$ninja_version"
        manifest_set AIRUNNER_CUDA_ENABLED "$ENABLE_CUDA"
        manifest_set AIRUNNER_CUDA_ARCHITECTURES "$cuda_arch"
        manifest_set AIRUNNER_CPU_BASELINE "GGML_NATIVE=OFF"
        if [[ "$TARGET_PLATFORM" == "windows" ]]; then
            manifest_set AIRUNNER_WINDOWS_RUNTIME_LINKAGE "static"
            manifest_set AIRUNNER_MINGW_GCC_RUNTIME_PACKAGE_VERSION "13.2.0-6ubuntu1+26.1"
            manifest_set AIRUNNER_MINGW_W64_PACKAGE_VERSION "11.0.1-3build1"
            manifest_set AIRUNNER_WINDOWS_RUNTIME_NOTICES "GPL-3.txt,mingw-gcc-runtime-copyright.txt,mingw-runtime-source.txt,winpthreads-COPYING.txt"
        fi
        manifest_set AIRUNNER_LLAMA_SERVER_SHA256 "$llama_hash"
        manifest_set AIRUNNER_WHISPER_SERVER_SHA256 "$whisper_hash"
    } >> "$manifest_path"

    cp "$PINS_FILE" "$pins_path"
}

version_line() {
    local output
    output="$("$@" 2>&1)" || return 1
    printf '%s\n' "${output%%$'\n'*}"
}

manifest_set() {
    printf '%s=%q\n' "$1" "$2"
}

sha256_file() {
    local digest
    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "sha256sum is required to record built binary hashes" >&2
        return 1
    fi
    read -r digest _ < <(sha256sum -- "$1")
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$digest"
}

build_llama_sidecar() {
    local source_path="$SOURCE_DIR/llama.cpp"
    local build_path="$BUILD_DIR/llama.cpp"

    fetch_repo \
        "llama.cpp" \
        "$LLAMA_CPP_REPOSITORY" \
        "$LLAMA_CPP_COMMIT"
    configure_build \
        "$source_path" \
        "$build_path" \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_TOOLS=ON \
        -DLLAMA_BUILD_WEBUI=OFF
    build_target "$build_path" "llama-server"
    copy_binary "$build_path" "llama-server"
}

build_whisper_sidecar() {
    local source_path="$SOURCE_DIR/whisper.cpp"
    local build_path="$BUILD_DIR/whisper.cpp"

    fetch_repo \
        "whisper.cpp" \
        "$WHISPER_CPP_REPOSITORY" \
        "$WHISPER_CPP_COMMIT"
    local generated="$source_path/bindings/javascript/package.json"
    if [[ -e "$generated" || -L "$generated" ]]; then
        echo "Refusing pre-existing Whisper generated source path: $generated" >&2
        return 1
    fi
    configure_build \
        "$source_path" \
        "$build_path" \
        -DWHISPER_BUILD_SERVER=ON \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_SDL2=OFF
    build_target "$build_path" "whisper-server"
    copy_binary "$build_path" "whisper-server"
    remove_whisper_generated_source_file
}

main() {
    parse_args "$@"
    detect_jobs >/dev/null
    load_pins
    if [[ "$OFFLINE" == "1" && "$CLEAN" == "1" ]]; then
        echo "--offline cannot be combined with --clean because clean removes the pinned source checkouts" >&2
        return 1
    fi
    configure_platform
    clean_target
    prepare_layout
    build_llama_sidecar
    build_whisper_sidecar
    copy_upstream_licenses
    copy_windows_runtime_licenses
    write_manifest

    echo
    echo "Built native runtime sidecars in: $OUTPUT_ROOT/$TARGET_PLATFORM"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
