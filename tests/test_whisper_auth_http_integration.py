"""Exercise the real cpp-httplib pre-routing and error-handler pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "whisper_auth_http_integration.cpp"
SOURCE = Path(
    os.environ.get(
        "AIRUNNER_WHISPER_AUTH_SOURCE",
        ROOT / "build/runtime-sidecars-work/source/whisper.cpp",
    )
)


def test_pre_routing_auth_survives_whisper_error_rewrite() -> None:
    auth_header = SOURCE / "examples/server/auth.hpp"
    if not auth_header.is_file():
        pytest.skip("patched whisper.cpp source is not present")
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable for isolated auth test")
    with tempfile.TemporaryDirectory(prefix="whisper-auth-http-") as temporary:
        binary = Path(temporary) / "auth-http-test"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-pthread",
                "-I",
                str(auth_header.parent),
                str(FIXTURE),
                "-o",
                str(binary),
            ],
            check=True,
            timeout=60,
        )
        subprocess.run([str(binary)], check=True, timeout=15)
