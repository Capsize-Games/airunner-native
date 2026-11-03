# airunner-native

Pinned native runtime sidecar (`llama.cpp` / `whisper.cpp`) build
tooling for
[Capsize-Games/airunner](https://github.com/Capsize-Games/airunner)
(AI Runner).

This repository builds and packages the native sidecars with CMake and Ninja,
using MinGW for Windows cross-compilation. Its scripts and tests include Python
code, but its runtime archives do not include the Python application or an
interpreter. Sidecar source pins, patches and toolchains are released separately
from the consuming application.

An earlier version of this repository also carried the Python launcher
that starts the desktop app (`airunner_native.launcher`,
`crash_handler`, `repo_paths`). That code turned out not to belong
here: it's tightly coupled to `airunner`/`airunner-services` (it
exists only to bootstrap them) and changes in lockstep with the rest
of the application, not on an independent cadence -- splitting it
added cross-repo coordination for no real benefit. It moved back into
`Capsize-Games/airunner`'s own `native/` directory.

## What's here

- `runtime_sidecars/runtime_pins.env` -- exact upstream commits for
  `llama.cpp` and `whisper.cpp`, so bundled runtime binaries don't
  drift with either project's own `master` branch.
- `scripts/build_runtime_sidecars.sh` -- builds both binaries for a
  given target platform.
- `.github/workflows/native-runtime-sidecars.yml` -- builds the
  `linux`/`windows` matrix on workflow dispatch and checks both archives on
  their native operating systems. Release promotion builds from an annotated
  tag, attaches the verified assets to a draft, and publishes it as immutable.
  Published-release events verify the existing release.
- `scripts/build_cuda_sm120_candidate.sh` and
  `scripts/package_cuda_sm120_candidate.py` -- build and hash-lock a separate
  Linux x86_64 CUDA SM 120a candidate. The package inputs are listed in
  `runtime_sidecars/cuda-sm120-package-lock.json`.

The existing release workflow does not build or publish the CUDA candidate.
It remains a separate artifact profile; the default Linux and Windows bundle
paths are unchanged.

## Build locally

```bash
./scripts/build_runtime_sidecars.sh --target-platform linux
```

Requires `cmake`, `mingw-w64`, and `ninja`. Output lands in
`build/runtime-sidecars/<platform>/{bin/,share/airunner/}`.

## Consuming a built bundle

`Capsize-Games/airunner`'s own release pipeline downloads a pinned
release from here rather than building sidecars itself -- see that
repository's `.github/native-sidecar-version` for which tag.

## Provenance

Extracted from `Capsize-Games/airunner` (issue
[#2196](https://github.com/Capsize-Games/airunner/issues/2196), part
of the repository-split tracker
[#2185](https://github.com/Capsize-Games/airunner/issues/2185)).
History for the moved files is preserved (`git filter-repo`).

## Licensing

GPL-3.0-only, matching the application this builds for. See
[`LICENSE`](LICENSE).
