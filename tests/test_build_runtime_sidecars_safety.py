"""Temporary filesystem checks for sidecar build-input safety helpers."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_runtime_sidecars.sh"


class SidecarSafetyTests(unittest.TestCase):
    def run_shell(self, body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        complete_env = os.environ.copy()
        if env:
            complete_env.update(env)
        return subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}"\n{body}'],
            cwd=REPO,
            env=complete_env,
            capture_output=True,
            text=True,
            check=False,
        )

    def valid_pins(self) -> str:
        return "\n".join(
            (
                "LLAMA_CPP_REPOSITORY=https://github.com/ggerganov/llama.cpp.git",
                "LLAMA_CPP_COMMIT=47a39665e7081dc482feec169961acc09750a5c4",
                "LLAMA_CPP_VERSION=b10000",
                "WHISPER_CPP_REPOSITORY=https://github.com/ggerganov/whisper.cpp.git",
                "WHISPER_CPP_COMMIT=9386f239401074690479731c1e41683fbbeac557",
                "WHISPER_CPP_VERSION=v1.8.4",
                "WHISPER_CPP_AUTH_PATCH_SHA256=a0d518cddd0b0ed1f679772109d30802d88d3148447d58d49c862aa2c5781697",
            )
        ) + "\n"

    def test_pin_payload_is_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pins = root / "pins.env"
            marker = root / "payload-ran"
            pins.write_text(
                self.valid_pins() + f"RUN_PAYLOAD=$(touch {marker})\n",
                encoding="utf-8",
            )
            result = self.run_shell(
                'PINS_FILE="$TEST_PINS"; load_pins',
                env={"TEST_PINS": str(pins)},
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_valid_pins_accept_numbered_sha256_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pins = Path(temporary) / "pins.env"
            pins.write_text(self.valid_pins(), encoding="utf-8")
            result = self.run_shell(
                'PINS_FILE="$TEST_PINS"; load_pins; '
                'test "$WHISPER_CPP_AUTH_PATCH_SHA256" = '
                '"a0d518cddd0b0ed1f679772109d30802d88d3148447d58d49c862aa2c5781697"',
                env={"TEST_PINS": str(pins)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_commit_and_unexpected_repository_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pins = Path(temporary) / "pins.env"
            for bad_field in (
                "LLAMA_CPP_COMMIT=main",
                "LLAMA_CPP_REPOSITORY=https://attacker.invalid/llama.cpp.git",
            ):
                lines = self.valid_pins().splitlines()
                field = bad_field.split("=", maxsplit=1)[0]
                lines = [
                    bad_field if line.startswith(field + "=") else line
                    for line in lines
                ]
                pins.write_text("\n".join(lines) + "\n", encoding="utf-8")
                result = self.run_shell(
                    'PINS_FILE="$TEST_PINS"; load_pins',
                    env={"TEST_PINS": str(pins)},
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unmarked_cleanup_target_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            work = root / "work"
            target = output / "linux"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            result = self.run_shell(
                f'OUTPUT_ROOT="{output}"; WORK_ROOT="{work}"; TARGET_PLATFORM=linux; CLEAN=1; clean_target',
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_unmarked_target_cannot_be_reused_without_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            work = root / "work"
            target = output / "linux"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            result = self.run_shell(
                f'OUTPUT_ROOT="{output}"; WORK_ROOT="{work}"; TARGET_PLATFORM=linux; prepare_layout',
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((target / "bin").exists())

    def test_owned_targets_clean_and_reinitialize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            work = root / "work"
            for target in (output / "linux", work / "linux"):
                target.mkdir(parents=True)
                (target / ".airunner-native-owned").write_text(
                    "airunner-native-output-v1:linux\n", encoding="utf-8"
                )
                (target / "stale").write_text("stale", encoding="utf-8")
            result = self.run_shell(
                f'OUTPUT_ROOT="{output}"; WORK_ROOT="{work}"; TARGET_PLATFORM=linux; CLEAN=1; clean_target; prepare_layout',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for target in (output / "linux", work / "linux"):
                self.assertTrue((target / ".airunner-native-owned").is_file())
                self.assertFalse((target / "stale").exists())

    def test_job_count_is_bounded_and_positive(self) -> None:
        for value, expected in (
            ("1", 0),
            ("64", 0),
            ("0", 1),
            ("65", 1),
            ("2x", 1),
            ("999999999999999999999", 1),
        ):
            result = self.run_shell(f'JOBS="{value}"; detect_jobs >/dev/null')
            self.assertEqual(result.returncode, expected, f"jobs={value}: {result.stderr}")

    def test_whisper_source_guard_rejects_unrelated_dirty_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "whisper"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            server = source / "examples/server/server.cpp"
            server.parent.mkdir(parents=True)
            server.write_text("upstream\n", encoding="utf-8")
            (source / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "examples/server/server.cpp", ".gitignore"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)

            clean = self.run_shell(
                'assert_whisper_source_clean_or_auth_patch "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)

            (source / "examples/server/auth.hpp").write_text("auth\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "-N", "examples/server/auth.hpp"], check=True)
            server.write_text("patched\n", encoding="utf-8")
            valid_patch = self.run_shell(
                'assert_whisper_source_auth_patch_only "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertEqual(valid_patch.returncode, 0, valid_patch.stderr)

            (source / "unrelated.tmp").write_text("unexpected\n", encoding="utf-8")
            rejected = self.run_shell(
                'assert_whisper_source_clean_or_auth_patch "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            (source / "unrelated.tmp").unlink()
            (source / "ignored.tmp").write_text("ignored but present\n", encoding="utf-8")
            ignored_rejected = self.run_shell(
                'assert_whisper_source_clean_or_auth_patch "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertNotEqual(ignored_rejected.returncode, 0, ignored_rejected.stdout + ignored_rejected.stderr)

    def test_binary_digest_is_computed_from_actual_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "sidecar"
            contents = b"fake sidecar bytes, not an executable\n"
            binary.write_bytes(contents)
            result = self.run_shell(f'sha256_file "{binary}"')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), hashlib.sha256(contents).hexdigest())

    def test_manifest_records_real_pins_toolchain_and_binary_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bin_dir = bundle / "bin"
            share_dir = bundle / "share" / "airunner"
            fake_tools = root / "tools"
            bin_dir.mkdir(parents=True)
            share_dir.mkdir(parents=True)
            fake_tools.mkdir()
            pins = root / "pins.env"
            pins.write_text(self.valid_pins(), encoding="utf-8")
            llama = b"test llama binary contents"
            whisper = b"test whisper binary contents"
            (bin_dir / "llama-server").write_bytes(llama)
            (bin_dir / "whisper-server").write_bytes(whisper)
            for command, output in {
                "cmake": "cmake version 3.test",
                "git": "git version 2.test",
                "cc": "cc test compiler",
                "c++": "cxx test compiler",
                "ninja": "1.test",
            }.items():
                wrapper = fake_tools / command
                wrapper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
                wrapper.chmod(0o755)
            env = {
                "TEST_BUNDLE": str(bundle),
                "TEST_PINS": str(pins),
                "PATH": f"{fake_tools}:{os.environ['PATH']}",
            }
            result = self.run_shell(
                'BUNDLE_SHARE_DIR="$TEST_BUNDLE/share/airunner"; '
                'BUNDLE_BIN_DIR="$TEST_BUNDLE/bin"; PINS_FILE="$TEST_PINS"; '
                'TARGET_PLATFORM=linux; BINARY_SUFFIX=""; CMAKE_BUILD_TYPE=Release; '
                'C_COMPILER=cc; CXX_COMPILER=c++; JOBS=3; ENABLE_CUDA=1; '
                'CUDA_ARCHITECTURES=89; '
                'LLAMA_CPP_REPOSITORY=https://github.com/ggerganov/llama.cpp.git; '
                'LLAMA_CPP_COMMIT=47a39665e7081dc482feec169961acc09750a5c4; '
                'LLAMA_CPP_VERSION=b10000; '
                'WHISPER_CPP_REPOSITORY=https://github.com/ggerganov/whisper.cpp.git; '
                'WHISPER_CPP_COMMIT=9386f239401074690479731c1e41683fbbeac557; '
                'WHISPER_CPP_VERSION=v1.8.4; '
                'WHISPER_CPP_AUTH_PATCH_SHA256=a0d518cddd0b0ed1f679772109d30802d88d3148447d58d49c862aa2c5781697; '
                'write_manifest',
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = (share_dir / "runtime_manifest.env").read_text(encoding="utf-8")
            self.assertIn("AIRUNNER_LLAMA_CPP_COMMIT=47a39665e7081dc482feec169961acc09750a5c4", manifest)
            self.assertIn("AIRUNNER_CMAKE_VERSION=cmake\\ version\\ 3.test", manifest)
            self.assertIn("AIRUNNER_BUILD_JOBS=3", manifest)
            self.assertIn("AIRUNNER_CUDA_ARCHITECTURES=89", manifest)
            self.assertIn("AIRUNNER_CPU_BASELINE=GGML_NATIVE=OFF", manifest)
            self.assertIn(f"AIRUNNER_LLAMA_SERVER_SHA256={hashlib.sha256(llama).hexdigest()}", manifest)
            self.assertIn(f"AIRUNNER_WHISPER_SERVER_SHA256={hashlib.sha256(whisper).hexdigest()}", manifest)
            self.assertIn(
                "AIRUNNER_WHISPER_CPP_AUTH_PATCH_SHA256=a0d518cddd0b0ed1f679772109d30802d88d3148447d58d49c862aa2c5781697",
                manifest,
            )
            self.assertTrue((share_dir / "runtime_pins.env").is_file())


if __name__ == "__main__":
    unittest.main()
