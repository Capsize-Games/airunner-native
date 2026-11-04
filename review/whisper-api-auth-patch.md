# whisper.cpp v1.8.4 API authentication patch

Target: `ggerganov/whisper.cpp` commit
`9386f239401074690479731c1e41683fbbeac557` (v1.8.4).

Patch: `runtime_sidecars/patches/whisper.cpp-v1.8.4-api-auth.patch`
SHA256: `a0d518cddd0b0ed1f679772109d30802d88d3148447d58d49c862aa2c5781697`.

The patch adds optional `--api-key-file PATH` support to the upstream server.
When provided, the server reads one bounded token line at startup and installs
an `httplib` pre-routing handler. Every method and path requires the matching
`Authorization: Bearer ...` header except the exact `GET` request path formed by
`--request-path` plus `/health`. The handler runs before all registered routes,
so unauthenticated `/inference`, `/load`, static content, `OPTIONS`, and unknown
paths receive 401 before a route can process them. Comparisons run over the
maximum input length. The server's error handler preserves the 401 status,
challenge header, and response body. No token is accepted as a command-line
value or written to logs.

The API key file option remains optional for loopback-only defaults. If the
server is configured to bind to a host other than `127.0.0.1`, `::1`, or
`localhost`, startup requires an API key file. AIRunner's managed service
always supplies its existing 0600 loopback-token file and continues to bind to
loopback. Health polling does not need a credential because only the precise
prefixed health GET is exempt.

## Applying the patch

`runtime_sidecars/runtime_pins.env` binds both the immutable upstream commit and
patch hash. `scripts/build_runtime_sidecars.sh` verifies the checkout HEAD and
patch SHA, applies the patch with `git apply --check`, and then verifies the
resulting diff hash before CMake configuration. It permits an already applied
patch only when the complete source worktree status contains the expected
`server.cpp` and `auth.hpp` changes; unrelated tracked, untracked, and ignored
files fail the guard. The original upstream commit is unchanged. Full source,
`LICENSE`, and notices remain in the source checkout.

## Review and checks

The owner-controlled mirror and exact private source worktree are recorded in
the private audit receipt. The review covered `examples/server/server.cpp`,
its route and CLI setup, `examples/server/httplib.h` pre-routing and error
handling, and upstream `LICENSE` (MIT). The bounded offline CPU build used the
pinned source, CMake archive, and Ubuntu 24 image; it did not access the
network or GPU.

`tests/whisper_auth_handler.cpp` checks key-file bounds and the pre-routing
helper. `tests/whisper_auth_http_integration.cpp` runs the actual `httplib`
pre-routing and error-handler pipeline without a model and verifies that
unauthorized inference/load routes return 401 without reaching their handlers.
The corrected full server was built and started with the reviewed local model
for health and auth-rejection probes. One authenticated local CPU transcription
succeeded on the immediately preceding patch digest; no transcription was run
after the error-handler correction. The latest HTTP probe found health 200 and
unauthorized inference/load 401, with the Bearer challenge present and no token
in logs or receipts. No GPU or deployment acceptance is claimed.
