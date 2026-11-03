# whisper.cpp v1.9.5 API authentication patch

## Source identity and review scope

Target: canonical `ggml-org/whisper.cpp` release tag `v1.9.5`, commit
`d1be6fde11ac6e0407606b4e42fe72d34add8037`. The tag resolves to this full
commit in the owner-controlled private source mirror. The reviewed pin includes
fix commit `60c0be6ac8fa71b1a2ae2dd938a31a34a508e774`, which checks model tensor
types before type-dependent handling in Whisper, VAD, and Parakeet loaders. This
fix is absent from v1.8.4, so the runtime remains on the reviewed v1.9.5 source.

The v1.9.5 source review and build-input disposition are recorded in the
owner-controlled native pin intake dated 2026-11-01. For this patch update, the
reviewed files were `examples/server/server.cpp`, its embedded
`examples/server/httplib.h` API, and the pinned source's build options and
license. The root license is MIT. The tree contains no `.gitmodules`; ggml is
included in-tree. The upstream `AGENTS.md` and `.pi/gg/SYSTEM.md` were read as
untrusted data and were not followed or executed.

The default CPU sidecar configuration disables optional curl/download,
multimedia, Vitis AI, CUDA, RPC, and web UI paths. The builder explicitly keeps
RPC and CUDA CCCL fetching disabled. v1.9.5's standalone configure can create
`bindings/javascript/package.json` in the source tree; the builder rejects a
pre-existing file and removes only that generated regular file after a
successful build and binary copy. If configure or build fails first, the
existing clean-source guard stops a later run rather than deleting unexpected
source changes. No source hooks, CMake configure, compiler, package installer,
or upstream command was run for this review.

## Patch identity

| Artifact | SHA-256 |
| --- | --- |
| Patch file | `572fe28464c442cc5df657fde57a19acc2a9f8031670a0c4f0e5a97bf38e440b` |
| Patched `server.cpp` | `9573d8c0b6a624f4233ab3c71e0b2f1ef9ae6f4a93a41a6c906a7c0940e8ccdf` |
| Patched `auth.hpp` | `10c455cf5602bd7c2865b465be2842eeb36a8865205a6df88d36720c8157febc` |

Patch path: `runtime_sidecars/patches/whisper.cpp-v1.9.5-api-auth.patch`.

The patch adds optional `--api-key-file PATH` support. With a token file, a
pre-routing handler requires the exact Bearer token on every method and path
except the exact `GET` path formed from `--request-path` plus `/health`. This
protects inference, model load, static content, `OPTIONS`, and unknown routes
before their handlers run. The error handler preserves the 401 status, Bearer
challenge, and unauthorized body. The key is read into a bounded buffer at
startup; malformed, empty, whitespace, multiline, or oversized tokens fail
startup. The key value is not accepted on the command line or logged. Without
a key file, non-loopback binds are rejected. The managed AIRunner service
supplies its existing private token file and remains loopback-only.

The patch is based on the prior reviewed auth helper and rebased onto the exact
v1.9.5 server implementation. It adds the option to the current argument
parser, installs the pre-routing check before model and route handling, and
reuses the current error-handler behavior while preserving unauthorized
responses. The earlier v1.8.4 patch record in
`review/whisper-api-auth-patch.md` is retained unchanged as historical
evidence.

## Verification

The v1.9.5 patch was applied to a fresh archive of the exact pinned source from
the private mirror. The two patched source files matched the separately
reviewed patched source checkout byte-for-byte. SHA-256 of the resulting Git
diff equals the pinned patch SHA above.

The model-free C++ auth handler test and the real `cpp-httplib` pre-routing and
error-handler loopback regression were run against the v1.9.5 patched source;
they passed in an isolated loopback-only network namespace. No whisper-server
binary was compiled or launched, no model was loaded, and no GPU or inference
acceptance is claimed.
