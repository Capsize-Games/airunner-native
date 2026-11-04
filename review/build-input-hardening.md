# Build input hardening review

Base reviewed: `v0.1.0`, commit `0554185f27ac7db8f58e1ae8d59ba041e4118e62`. This patch changes only the sidecar build script, its runtime-sidecar README, and focused standard-library tests. No upstream source was fetched or built; no packages, models, GPUs, release assets, or production services were used.

## Changes

- Parse the six allowed pin fields as inert data. Reject shell syntax, duplicate/unknown/missing fields, repository URL changes, malformed full commit IDs, and invalid version labels before any repository commands run.
- Restrict cleanup and reuse to target directories bearing the script's ownership marker. Refuse symlink targets and pre-existing unmarked target directories; `--clean` validates both output and work targets before removing either.
- Replace host-wide `nproc` with a fixed four-job default and an explicit 1–64 `--jobs`/`AIRUNNER_BUILD_JOBS` bound.
- Pass `GGML_NATIVE=OFF` for a consistent CPU baseline.
- Record both exact source pins, target/build settings, compiler and tool versions, requested CUDA architecture (or `compiler-default`), CPU baseline, and SHA-256 digests of the generated server binaries in `runtime_manifest.env`.
- Correct the README's llama.cpp pin to match `runtime_pins.env`; document build limits, cleanup ownership, and the manifest contents.

The manifest digests describe built files; they do not authenticate an archive or replace an independently trusted release digest/signature. No signing or upstream synchronization was added.

## Checks

Ran `bash -n scripts/build_runtime_sidecars.sh` successfully. Ran `python3 -m unittest discover -s tests -p 'test_build_runtime_sidecars_safety.py' -v`: 8 passed. Tests use temporary directories and fake version commands; they cover inert pin parsing (including payload non-execution), invalid pins, refusal to delete or reuse unmarked targets, clean/reinitialization of marked targets, bounded job input, and manifest contents/digests from temporary fake binaries. No sidecar build was run.
