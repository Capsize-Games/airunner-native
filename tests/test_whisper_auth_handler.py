"""Compile the patched auth helper without building Whisper or loading models."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "whisper_auth_handler.cpp"
SOURCE = Path(
    os.environ.get(
        "AIRUNNER_WHISPER_AUTH_SOURCE",
        ROOT / "build/runtime-sidecars-work/source/whisper.cpp",
    )
)


def test_patched_whisper_auth_handler_without_model_or_gpu() -> None:
    auth_header = SOURCE / "examples/server/auth.hpp"
    if not auth_header.is_file():
        pytest.skip("patched whisper.cpp source is not present")
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable for isolated auth test")
    with tempfile.TemporaryDirectory(prefix="whisper-auth-handler-") as temporary:
        binary = Path(temporary) / "auth-handler-test"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-I",
                str(auth_header.parent),
                str(FIXTURE),
                "-o",
                str(binary),
            ],
            check=True,
            timeout=60,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
