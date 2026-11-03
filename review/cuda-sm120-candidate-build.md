# CUDA SM 120 llama-server candidate build

This is a separate Linux candidate build path for issue #2416. It does not
replace or modify the `v0.2.3` CPU sidecar, its Whisper binary, or any published
artifact. The candidate is not a release and requires explicit consumer
selection in a later change.

## Reviewed source and license

- Source: `https://github.com/ggml-org/llama.cpp`, commit
  `7acb4e8cd2ce21f457d1298e75fad729520d263c` (tag `b9523`). This is the exact
  source revision recorded for the reviewed `llama-cpp-python` CUDA build.
- The producer builder requires a clean local Git checkout at that exact
  commit. It does not fetch source or run Git inside the build container.
- The upstream source review identified the CUDA CCCL `FetchContent` path as
  avoidable for this build. The candidate disables `GGML_CUDA_CUB_3DOT2` and
  sets `FETCHCONTENT_FULLY_DISCONNECTED=ON`; the source and CMake configure/build
  run with container networking disabled.
- llama.cpp is MIT licensed. The candidate copies the source `LICENSE` into the
  output as `LICENSE-llama.cpp.txt` and records the source commit in
  `candidate-provenance.env`.

## Build inputs and configuration

- Build image:
  `nvidia/cuda:12.9.2-devel-ubuntu24.04@sha256:16656a1ef115bca9e1f820c6349876f1486d2b3c9a0e615773799fe402960dc5`.
- CMake: the builder accepts the Kitware 3.31.6 archive and verifies its SHA-256
  `5a1133ff103c71eb5120e2cc3de922733e7d8a26a98ae716397e8676adb367bf`;
  it extracts the verified archive into a temporary directory and also checks
  the `bin/cmake` SHA-256
  `c4b3b237dc7a013590db9e90f70fca2dfdedde09521e920d3339569ea364230a`.
  Both checks finish before Docker starts.
- CUDA architectures are pinned to `120a-real` in both
  `GGML_CUDA_ARCHITECTURES` and `CMAKE_CUDA_ARCHITECTURES`.
- `GGML_CUDA=ON`, `GGML_NATIVE=OFF`, `GGML_CUDA_NCCL=OFF`, `LLAMA_CURL=OFF`,
  web UI/app and tests disabled, and only `llama-server` is built.
- Since the pinned image has no Git executable, build number `9523` is checked
  against the exact source commit count and both llama.cpp and ggml commit
  strings are supplied explicitly from the reviewed commit. A failed initial
  attempt produced `version: 0 (unknown)` despite compiling the full target;
  the retry reuses that work tree and rebuilds only targets affected by the
  metadata change.
- Container is `linux/amd64`, network-disabled, read-only except `/tmp`, build
  and candidate-output mounts, with 2 CPUs, 8 GiB memory, and at most 8 build
  jobs (default 2). Runtime is explicitly `runc`; the CUDA driver stub is used
  for link-time discovery, and no GPU device is mounted.
- Candidate output is isolated under
  `build/runtime-sidecars-candidates/cuda-sm120`. It includes the server,
  license, server SHA-256, source/build provenance, and version output.

## Maintained MTP offload correction

The unpatched hosted candidate `41720615159` loaded the Qwen3.5-9B Q8 model
at context 32768, but reported 33/34 GPU layers and assigned model block 0 to
the CPU. The source loader counts `n_layer_all`, including its appended
next-token prediction block, while negative GPU-layer requests used the
smaller main-stack count. This prevents a full-offload acceptance claim.

`runtime_sidecars/patches/llama.cpp-cuda-all-mtp-offload.patch` changes that
negative branch to `hparams.n_layer_all + 1`. Nonnegative requests are unchanged.
Its SHA-256 is
`09ce5a6113a69416dfa721f4be19c4d70f75c4b36028ece9926786c5bba814a4`.
The same return expression was inspected in the upstream function at
[`149f5641b931aaf33697d70f9abc90c3e5300247`](https://github.com/ggml-org/llama.cpp/blob/149f5641b931aaf33697d70f9abc90c3e5300247/src/llama-model.cpp#L2372).
This attributes the examined source; it does not identify the introducing commit
or adopt the rest of that revision. No duplicate upstream report is needed.

The producer archives the pristine pinned checkout into its owned work area,
applies only the hash-pinned patch, and leaves the supplied checkout unchanged.
The lock, producer recipe, build receipt, archive and promotion checks bind the
patch identity. The archive retains the patch beside its build recipe. A small
C++ regression applies the real patch and checks the changed expression for
MTP/non-MTP, negative, CPU and positive layer requests without loading a model.
Actual patched CUDA compilation and model/API/cleanup acceptance remain required.

Device acceptance can correlate server-selected PCI identity with driver
inventory and the owned server PID's GPU UUID. The public receipt retains only
the device name, physical compute capability and private-observation digest;
compiled CUDA architecture alone is not device evidence.

## Earlier unpatched candidate evidence

The build recipe checks the exact source commit and clean working tree, the
source tag's commit count, exact CMake archive and executable digests, CUDA and
build-metadata values in `CMakeCache.txt`, server version output containing the
pinned full source revision, and server help output. It disables RPC explicitly,
rejects overlapping output/work/input/config paths, requires fresh candidate
output and a matching owned work tree for incremental retries, and records a
running, failed, or successful status with provenance. Container output is
saved as `build.log`.

The first complete compile linked `llama-server` and all CUDA objects, then
failed the post-build version check: the CUDA image has no Git executable and
the binary reported `version: 0 (unknown)`. That failed result and its complete
log are preserved in the first attempt output. The retry reused the same CMake
build tree, supplied `LLAMA_BUILD_NUMBER=9523`, `LLAMA_BUILD_COMMIT` and
`GGML_BUILD_COMMIT` from the exact source pin, and rebuilt only metadata-bearing
libraries and the server. It completed successfully. A follow-on incremental
relink added `$ORIGIN/../lib` as the server RUNPATH. The final server reports
`version: 9523 (7acb4e8cd2ce21f457d1298e75fad729520d263c)` and has SHA-256
`102c2b26a0a74bc3ed580f7787aef1b046eeef8e496f4f453bf275a33c9a1a84`.

`flags.make` records NVCC
`--generate-code=arch=compute_120a,code=[sm_120a]`. `cuobjdump --list-elf` on
`libggml-cuda.a` found 138 cubins, each named for `sm_120a`. The reproducible
package script assembled the candidate archive at
`/tmp/airunner-native-linux-x86_64-cuda-sm120-candidate-final-20261103.tar.gz`
(SHA-256
`24a89c1c0d2cf934c9809061f514dd8e833d83be432ee4bd31d5ce345e3509ec`). It
contains the server, exact V5-selected wheel members for `libcudart`, cuBLAS,
cuBLASLt and NVRTC, their full NVIDIA license texts, and build/source receipts.
Static dependency inspection found cuBLASLt's `dlopen` reference to
`libnvrtc.so.12` and NVRTC's reference to `libnvrtc-builtins.so.12.9`; both
libraries are included. The selected wheel's `libnvrtc.so.12` has RUNPATH
`$ORIGIN:` with a trailing empty component. The archive launcher sets
`LD_LIBRARY_PATH` to the artifact library directory and changes the child
working directory to `/` before execution. The candidate declares the verified
Ubuntu 24.04 `libgomp1` and GNU runtime packages and host-provided
`libcuda.so.1` as external dependencies. No driver or stub library is included.

Byte comparison against the pinned CUDA 12.9.2 image found that its
`libcudart.so.12.9.79`, `libnvrtc-builtins.so.12.9.86`, and
`libnvJitLink.so.12.9.86` bytes match the selected V5 wheels. Image cuBLAS,
cuBLASLt, and `libnvrtc.so.12.9.86` bytes differ from the selected wheels, so
the archive uses wheel members with preserved license texts. NVRTC/cuBLAS
version compatibility has not been tested on a GPU.

The external archive SHA-256 receipt and all internal `SHA256SUMS` entries
verified; the file manifest lists 31 payload files. The seven package tests
cover wheel hash rejection before extraction, traversal and symlink rejection,
target-platform validation, deterministic archives, failed-build rejection,
and protection of the CPU bundle output path.

Shell syntax validation passed. The four candidate safety tests passed; the
existing producer build-input safety tests passed (8 tests). Both build
containers used `runc`, `network=none`, and no GPU device requests. Inspection
of `--version` used only the CUDA link stub and emitted the expected stub-driver
warning. No model was loaded and no inference or GPU execution was performed.
The candidate archive is local and unpublished; no release or consumer
selection change was made.
