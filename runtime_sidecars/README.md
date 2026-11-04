# Native Runtime Sidecars

This directory defines the pinned AIRunner build inputs for the native
runtime sidecars shipped with packaged installs.

## Pinned Upstreams

The authoritative pins live in `runtime_pins.env`:

- `llama.cpp` commit `47a39665e7081dc482feec169961acc09750a5c4`
  (`b10000`)
- `whisper.cpp` commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`
  (`v1.9.5`), with the separately hash-pinned API authentication patch

These are exact commit hashes so AIRunner's runtime bundles do not drift
with upstream `master`.

## Build Flow

Build Linux sidecar binaries (fetching the exact pinned commits when needed):

```bash
./scripts/build_runtime_sidecars.sh --clean --target-platform linux
```

Build CUDA-enabled Linux sidecars for a specific architecture target:

```bash
./scripts/build_runtime_sidecars.sh \
  --clean \
  --target-platform linux \
  --enable-cuda \
  --cuda-architectures 89
```

Build from pre-fetched, clean pinned checkouts without contacting Git remotes:

```bash
./scripts/build_runtime_sidecars.sh --offline --target-platform linux
```

Cross-build Windows sidecar binaries from Linux when MinGW-w64 is present:

```bash
./scripts/build_runtime_sidecars.sh --clean --target-platform windows
```

The build script also honors:

- `AIRUNNER_SIDECAR_ENABLE_CUDA=1`
- `AIRUNNER_SIDECAR_CUDA_ARCHITECTURES=<arch list>`

Builds use four parallel jobs by default. Set `--jobs` or
`AIRUNNER_BUILD_JOBS` to a value from 1 through 64 to change that limit.
`--clean` only removes a target directory created by this script and marked
as owned. Existing unmarked target directories are left intact and cause the
build to stop; move or rename them yourself before using that target.

The script produces a bundle-style layout under:

- `build/runtime-sidecars/linux/`
- `build/runtime-sidecars/windows/`

Each output contains:

- `bin/llama-server` or `bin/llama-server.exe`
- `bin/whisper-server` or `bin/whisper-server.exe`
- `share/airunner/runtime_manifest.env`
- `share/airunner/runtime_pins.env`
- `share/airunner/producer-recipe.json`

The manifest records the upstream commits, target platform, selected CUDA
architecture (or `compiler-default`), compiler/build-tool version strings,
the `GGML_NATIVE=OFF` CPU baseline, and SHA-256 digests of both output
binaries. The bundle also includes the upstream MIT license texts in
`share/airunner/licenses/`. It describes the build that produced those files;
it does not authenticate a downloaded release archive.

Manifest version 2 records standalone native runtime facts. Binary paths are
relative to `share/airunner/runtime_manifest.env`, as declared by
`AIRUNNER_NATIVE_PATH_BASE=manifest-directory`. The archive contains no Python
interpreter or application entry point. The earlier manifest's Python and
application path fields are omitted; existing published archives are unchanged.

Each build also records `share/airunner/producer-recipe.json`. The build and
release packager verify that receipt against the committed source tree and the
hashes of the recipe inputs. The Whisper authentication patch is pinned by
SHA-256 in `runtime_pins.env`; the build checks its exact source commit and
resulting diff before compiling. See
`../review/whisper-api-auth-patch-v1.9.5.md` for current behavior and review
scope. `../review/whisper-api-auth-patch.md` preserves the earlier v1.8.4
record.

## CI

The `.github/workflows/native-runtime-sidecars.yml` workflow runs the same
script in a Linux and Windows matrix so CI can reproduce the pinned runtime
artifacts.
