"""Safety checks for the isolated CUDA SM 120 candidate entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_cuda_sm120_candidate.sh"


class CandidateBuildSafetyTests(unittest.TestCase):
    def test_shell_syntax_and_help_are_available_without_building(self) -> None:
        syntax = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        script_text = SCRIPT.read_text(encoding="utf-8")
        start = script_text.index("container_script='") + len("container_script='")
        end = script_text.index("'\n\nif [[ \"$DRY_RUN\"", start)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as nested:
            nested.write(script_text[start:end])
            nested.flush()
            nested_syntax = subprocess.run(
                ["bash", "-n", nested.name], capture_output=True, text=True
            )
        self.assertEqual(nested_syntax.returncode, 0, nested_syntax.stderr)
        help_result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("CUDA SM 120", help_result.stdout)
        self.assertIn("does not build Whisper", help_result.stdout)

    def test_modified_source_pin_is_rejected_before_output_or_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied_script = root / "repo" / "scripts" / SCRIPT.name
            copied_lock = root / "repo" / "runtime_sidecars" / "cuda-sm120-candidate.env"
            copied_script.parent.mkdir(parents=True)
            copied_lock.parent.mkdir(parents=True)
            shutil.copy2(SCRIPT, copied_script)
            lock_text = (REPO / "runtime_sidecars" / "cuda-sm120-candidate.env").read_text()
            copied_lock.write_text(lock_text.replace(
                "7acb4e8cd2ce21f457d1298e75fad729520d263c",
                "47a39665e7081dc482feec169961acc09750a5c4",
                1,
            ))

            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "--allow-empty",
                            "-m", "fixture"], check=True, capture_output=True)
            cmake_archive = root / "cmake.tar.gz"
            cmake_archive.write_bytes(b"not the pinned archive")
            output = root / "candidate-output"
            env = dict(os.environ)
            env["PATH"] = f"{root / 'missing-docker'}:{env['PATH']}"
            result = subprocess.run(
                [str(copied_script), "--source", str(source), "--cmake-archive", str(cmake_archive),
                 "--output-root", str(output), "--work-root", str(root / "work"), "--dry-run"],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected source pin", result.stderr)
            self.assertFalse(output.exists())

    def test_build_guards_are_explicit_and_provenance_tracks_failure(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(
            source.index('cmake_hash="$(sha256sum "$cmake_real/bin/cmake"'),
            source.index('"$cmake_real/bin/cmake" --version'),
        )
        for required in (
            "--runtime=runc",
            "--network=none",
            "--runtime=runc",
            "-DGGML_RPC=OFF",
            "-DGGML_CUDA_ARCHITECTURES=120a-real",
            "-DCMAKE_CUDA_ARCHITECTURES=120a-real",
            "-DLLAMA_BUILD_COMMIT=\"$LOCK_LLAMA_CPP_COMMIT\"",
            "-DLLAMA_BUILD_NUMBER=\"$LOCK_LLAMA_BUILD_NUMBER\"",
            "-DGGML_BUILD_COMMIT=\"$LOCK_LLAMA_CPP_COMMIT\"",
            "cuobjdump --list-elf",
            "cuda-compile-flags.make",
            "elf-cuda-inspection.txt",
            'test "$cubin_count" = 138',
            "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
            r"-DCMAKE_INSTALL_RPATH=\$ORIGIN/../lib",
            r"-DCMAKE_BUILD_RPATH=\$ORIGIN/../lib",
            "status=running",
            "status=failed",
            "source_build_number=$source_build_number",
            'tee "$OUTPUT_ROOT/build.log"',
            "paths_overlap",
            '"$(realpath -m "$ROOT_DIR/.git")"',
            '"$(realpath -m "$ROOT_DIR/.github")"',
            '"$(realpath -m "$ROOT_DIR/runtime_sidecars")"',
        ):
            self.assertIn(required, source)
        self.assertNotIn("--gpus", source)

    def test_path_overlap_helper_detects_equal_and_nested_paths(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("paths_overlap() {")
        end = source.index("\n}\n", start) + len("\n}\n")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as helper:
            helper.write(source[start:end])
            helper.flush()
            for left, right, expected in (
                ("/tmp/candidate", "/tmp/candidate", 0),
                ("/tmp/candidate/work", "/tmp/candidate", 0),
                ("/tmp/candidate", "/tmp/source", 1),
            ):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; paths_overlap "$2" "$3"',
                     "test", helper.name, left, right],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
