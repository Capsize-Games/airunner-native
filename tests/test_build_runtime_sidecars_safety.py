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
WINDOWS_IMPORT_CHECK = REPO / "scripts" / "check_windows_runtime_imports.sh"


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
                "WHISPER_CPP_COMMIT=d1be6fde11ac6e0407606b4e42fe72d34add8037",
                "WHISPER_CPP_VERSION=v1.9.5",
                "WHISPER_CPP_AUTH_PATCH_SHA256=572fe28464c442cc5df657fde57a19acc2a9f8031670a0c4f0e5a97bf38e440b",
            )
        ) + "\n"

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
            valid = self.run_shell(
                'assert_whisper_source_auth_patch_only "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            (source / "unrelated.tmp").write_text("unexpected\n", encoding="utf-8")
            rejected = self.run_shell(
                'assert_whisper_source_clean_or_auth_patch "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertNotEqual(rejected.returncode, 0)
            (source / "unrelated.tmp").unlink()
            (source / "ignored.tmp").write_text("ignored\n", encoding="utf-8")
            ignored = self.run_shell(
                'assert_whisper_source_clean_or_auth_patch "$TEST_SOURCE"',
                env={"TEST_SOURCE": str(source)},
            )
            self.assertNotEqual(ignored.returncode, 0)

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

    def test_windows_platform_probe_does_not_write_into_unowned_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            work = root / "caller-work"
            gomp = root / "libgomp.a"
            gomp.write_bytes(b"fixture archive")
            for name in (
                "x86_64-w64-mingw32-g++-posix",
                "x86_64-w64-mingw32-gcc-posix",
            ):
                tool = tools / name
                tool.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = -print-file-name=libgomp.a ]; then "
                    "printf '%s\\n' \"$TEST_GOMP\"; fi\n",
                    encoding="utf-8",
                )
                tool.chmod(0o755)
            result = self.run_shell(
                f'TARGET_PLATFORM=windows; WORK_ROOT="{work}"; configure_platform',
                env={"PATH": f"{tools}:{os.environ['PATH']}", "TEST_GOMP": str(gomp)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(work.exists())

    def test_windows_executables_link_mingw_runtime_statically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary) / "tools"
            tools.mkdir()
            gomp = Path(temporary) / "libgomp.a"
            gomp.write_bytes(b"fixture archive")
            for name in (
                "x86_64-w64-mingw32-g++-posix",
                "x86_64-w64-mingw32-gcc-posix",
            ):
                tool = tools / name
                tool.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = -print-file-name=libgomp.a ]; then "
                    "printf '%s\\n' \"$TEST_GOMP\"; fi\n",
                    encoding="utf-8",
                )
                tool.chmod(0o755)
            result = self.run_shell(
                'TARGET_PLATFORM=windows; configure_platform; '
                'printf "%s\\n" "${CMAKE_PLATFORM_ARGS[@]}" "${CMAKE_OPENMP_ARGS[@]}"; '
                'DRY_RUN=1; CMAKE_CUDA_ARGS=(); configure_build source build',
                env={"PATH": f"{tools}:{os.environ['PATH']}", "TEST_GOMP": str(gomp)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("-DCMAKE_EXE_LINKER_FLAGS=-static", result.stdout)
            self.assertIn(f"-DOpenMP_gomp_LIBRARY={gomp}", result.stdout)

    def test_windows_runtime_notice_inputs_are_copied_into_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            notices = root / "runtime_sidecars" / "licenses"
            notices.mkdir(parents=True)
            expected = (
                "GPL-3.txt",
                "mingw-gcc-runtime-copyright.txt",
                "mingw-runtime-source.txt",
                "winpthreads-COPYING.txt",
            )
            for name in expected:
                (notices / name).write_text(f"fixture notice: {name}\n", encoding="utf-8")
            tools = root / "tools"
            tools.mkdir()
            dpkg_query = tools / "dpkg-query"
            dpkg_query.write_text(
                "#!/bin/sh\ncase \"$3\" in\n"
                "  gcc-mingw-w64-x86-64-posix-runtime|gcc-mingw-w64-x86-64-posix|g++-mingw-w64-x86-64-posix) printf '%s' '13.2.0-6ubuntu1+26.1' ;;\n"
                "  mingw-w64-x86-64-dev|mingw-w64-common) printf '%s' '11.0.1-3build1' ;;\n"
                "  *) exit 1 ;;\nesac\n",
                encoding="utf-8",
            )
            dpkg_query.chmod(0o755)
            share = root / "bundle" / "share" / "airunner"
            result = self.run_shell(
                'ROOT_DIR="$TEST_ROOT"; BUNDLE_SHARE_DIR="$TEST_SHARE"; '
                'TARGET_PLATFORM=windows; DRY_RUN=0; copy_windows_runtime_licenses',
                env={"TEST_ROOT": str(root), "TEST_SHARE": str(share), "PATH": f"{tools}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in expected:
                self.assertEqual(
                    (share / "licenses" / name).read_text(encoding="utf-8"),
                    f"fixture notice: {name}\n",
                )

    def test_windows_runtime_license_copy_rejects_unreviewed_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            dpkg_query = tools / "dpkg-query"
            dpkg_query.write_text("#!/bin/sh\nprintf '%s' 'different-version'\n", encoding="utf-8")
            dpkg_query.chmod(0o755)
            result = self.run_shell(
                'TARGET_PLATFORM=windows; DRY_RUN=0; copy_windows_runtime_licenses',
                env={"PATH": f"{tools}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("notice input mismatch", result.stderr)

    def test_windows_manifest_records_static_runtime_notice_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bin_dir = bundle / "bin"
            share_dir = bundle / "share" / "airunner"
            tools = root / "tools"
            bin_dir.mkdir(parents=True)
            share_dir.mkdir(parents=True)
            tools.mkdir()
            pins = root / "pins.env"
            pins.write_text(self.valid_pins(), encoding="utf-8")
            (bin_dir / "llama-server.exe").write_bytes(b"llama")
            (bin_dir / "whisper-server.exe").write_bytes(b"whisper")
            for command, output in {
                "cmake": "cmake version 3.test",
                "git": "git version 2.test",
                "cc": "cc test compiler",
                "c++": "cxx test compiler",
            }.items():
                wrapper = tools / command
                wrapper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
                wrapper.chmod(0o755)
            result = self.run_shell(
                'BUNDLE_SHARE_DIR="$TEST_BUNDLE/share/airunner"; '
                'BUNDLE_BIN_DIR="$TEST_BUNDLE/bin"; PINS_FILE="$TEST_PINS"; '
                'TARGET_PLATFORM=windows; BINARY_SUFFIX=.exe; CMAKE_BUILD_TYPE=Release; '
                'C_COMPILER=cc; CXX_COMPILER=c++; JOBS=2; ENABLE_CUDA=0; '
                'CUDA_ARCHITECTURES=; LLAMA_CPP_REPOSITORY=https://github.com/ggerganov/llama.cpp.git; '
                'LLAMA_CPP_COMMIT=47a39665e7081dc482feec169961acc09750a5c4; LLAMA_CPP_VERSION=b10000; '
                'WHISPER_CPP_REPOSITORY=https://github.com/ggerganov/whisper.cpp.git; '
                'WHISPER_CPP_COMMIT=d1be6fde11ac6e0407606b4e42fe72d34add8037; '
                'WHISPER_CPP_VERSION=v1.9.5; '
                'WHISPER_CPP_AUTH_PATCH_SHA256=572fe28464c442cc5df657fde57a19acc2a9f8031670a0c4f0e5a97bf38e440b; '
                'write_manifest',
                env={
                    "TEST_BUNDLE": str(bundle),
                    "TEST_PINS": str(pins),
                    "PATH": f"{tools}:{os.environ['PATH']}",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = (share_dir / "runtime_manifest.env").read_text(encoding="utf-8")
            self.assertIn("AIRUNNER_WINDOWS_RUNTIME_LINKAGE=static", manifest)
            self.assertIn("AIRUNNER_MINGW_GCC_RUNTIME_PACKAGE_VERSION=13.2.0-6ubuntu1+26.1", manifest)
            self.assertIn("AIRUNNER_MINGW_W64_PACKAGE_VERSION=11.0.1-3build1", manifest)
            self.assertIn(
                "AIRUNNER_WINDOWS_RUNTIME_NOTICES=GPL-3.txt\\,mingw-gcc-runtime-copyright.txt\\,"
                "mingw-runtime-source.txt\\,winpthreads-COPYING.txt",
                manifest,
            )

    def test_windows_import_check_accepts_system_dlls_and_rejects_mingw_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "bin"
            tools.mkdir()
            imports = root / "imports.txt"
            objdump = tools / "x86_64-w64-mingw32-objdump"
            objdump.write_text(
                "#!/bin/sh\ncat \"$TEST_IMPORTS\"\n", encoding="utf-8"
            )
            objdump.chmod(0o755)
            executable = root / "sidecar.exe"
            executable.write_bytes(b"PE fixture")
            imports.write_text(
                "DLL Name: KERNEL32.dll\nDLL Name: api-ms-win-core-file-l1-1-0.dll\n",
                encoding="utf-8",
            )
            env = {
                "PATH": f"{tools}:{os.environ['PATH']}",
                "TEST_IMPORTS": str(imports),
            }
            accepted = subprocess.run(
                [str(WINDOWS_IMPORT_CHECK), str(executable)],
                env={**os.environ, **env}, capture_output=True, text=True, check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

            imports.write_text(
                "DLL Name: KERNEL32.dll\nDLL Name: libstdc++-6.dll\n",
                encoding="utf-8",
            )
            rejected = subprocess.run(
                [str(WINDOWS_IMPORT_CHECK), str(executable)],
                env={**os.environ, **env}, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Unreviewed non-system runtime import", rejected.stderr)

    def test_offline_fetch_accepts_exact_clean_local_git_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "sources" / "fixture"
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Fixture"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "fixture@example.invalid"], check=True)
            (checkout / "LICENSE").write_text("fixture license\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "LICENSE"], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
            commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            repository = "https://github.com/ggerganov/llama.cpp.git"
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", repository], check=True)
            result = self.run_shell(
                'OFFLINE=1; DRY_RUN=0; SOURCE_DIR="$TEST_SOURCES"; fetch_repo fixture "$TEST_REPOSITORY" "$TEST_COMMIT"',
                env={"TEST_SOURCES": str(root / "sources"), "TEST_REPOSITORY": repository, "TEST_COMMIT": commit},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip(), commit)
            self.assertEqual(subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True), "")

    def test_source_verifier_rejects_dirty_and_wrong_origin_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "sources" / "fixture"
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Fixture"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "fixture@example.invalid"], check=True)
            (checkout / "file").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "file"], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
            commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            repository = "https://github.com/ggerganov/llama.cpp.git"
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", repository], check=True)
            env = {"TEST_SOURCES": str(root / "sources"), "TEST_REPOSITORY": repository, "TEST_COMMIT": commit}
            (checkout / "untracked").write_text("unexpected\n", encoding="utf-8")
            dirty = self.run_shell('OFFLINE=1; SOURCE_DIR="$TEST_SOURCES"; fetch_repo fixture "$TEST_REPOSITORY" "$TEST_COMMIT"', env=env)
            self.assertNotEqual(dirty.returncode, 0, dirty.stdout + dirty.stderr)
            (checkout / "untracked").unlink()
            (checkout / ".ignored-fixture").write_text("ignored\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "config", "core.excludesFile", str(root / "ignore")], check=True)
            (root / "ignore").write_text(".ignored-fixture\n", encoding="utf-8")
            ignored = self.run_shell('OFFLINE=1; SOURCE_DIR="$TEST_SOURCES"; fetch_repo fixture "$TEST_REPOSITORY" "$TEST_COMMIT"', env=env)
            self.assertNotEqual(ignored.returncode, 0, ignored.stdout + ignored.stderr)
            (checkout / ".ignored-fixture").unlink()
            wrong_origin = self.run_shell('OFFLINE=1; SOURCE_DIR="$TEST_SOURCES"; fetch_repo fixture https://example.invalid/source.git "$TEST_COMMIT"', env=env)
            self.assertNotEqual(wrong_origin.returncode, 0, wrong_origin.stdout + wrong_origin.stderr)

    def test_upstream_licenses_are_copied_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sources"
            output = root / "share" / "airunner"
            payloads = {"llama.cpp": b"fixture llama MIT terms\n", "whisper.cpp": b"fixture whisper MIT terms\n"}
            for name, contents in payloads.items():
                path = source / name / "LICENSE"
                path.parent.mkdir(parents=True)
                path.write_bytes(contents)
            result = self.run_shell(
                'DRY_RUN=0; SOURCE_DIR="$TEST_SOURCE"; BUNDLE_SHARE_DIR="$TEST_OUTPUT"; copy_upstream_licenses',
                env={"TEST_SOURCE": str(source), "TEST_OUTPUT": str(output)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, contents in payloads.items():
                self.assertEqual((output / "licenses" / f"{name}-LICENSE").read_bytes(), contents)

    def test_upstream_license_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sources"
            llama_license = source / "llama.cpp" / "LICENSE"
            llama_license.parent.mkdir(parents=True)
            actual = root / "actual"
            actual.write_text("fixture license\n", encoding="utf-8")
            llama_license.symlink_to(actual)
            whisper_license = source / "whisper.cpp" / "LICENSE"
            whisper_license.parent.mkdir(parents=True)
            whisper_license.write_text("fixture license\n", encoding="utf-8")
            result = self.run_shell(
                'DRY_RUN=0; SOURCE_DIR="$TEST_SOURCE"; BUNDLE_SHARE_DIR="$TEST_OUTPUT"; copy_upstream_licenses',
                env={"TEST_SOURCE": str(source), "TEST_OUTPUT": str(root / "output")},
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_mode_rejects_clean_before_touching_work_roots(self) -> None:
        result = self.run_shell('main --offline --clean')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cannot be combined", result.stderr)

    def test_cmake_flags_disable_rpc_and_optional_cccl_fetch(self) -> None:
        result = self.run_shell('DRY_RUN=1; CMAKE_PLATFORM_ARGS=(); CMAKE_CUDA_ARGS=(); configure_build source build')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-DGGML_RPC=OFF", result.stdout)
        self.assertIn("-DGGML_CUDA_CUB_3DOT2=OFF", result.stdout)

    def test_full_dry_run_does_not_create_output_or_work_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "not-created-output"
            work = root / "not-created-work"
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--clean", "--target-platform", "linux",
                 "--output-root", str(output), "--work-root", str(work)],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("git clone", result.stdout)
            self.assertIn("cmake", result.stdout)
            self.assertFalse(output.exists())
            self.assertFalse(work.exists())

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
                'WHISPER_CPP_COMMIT=d1be6fde11ac6e0407606b4e42fe72d34add8037; '
                'WHISPER_CPP_VERSION=v1.9.5; '
                'WHISPER_CPP_AUTH_PATCH_SHA256=572fe28464c442cc5df657fde57a19acc2a9f8031670a0c4f0e5a97bf38e440b; '
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
                "AIRUNNER_WHISPER_CPP_AUTH_PATCH_SHA256=572fe28464c442cc5df657fde57a19acc2a9f8031670a0c4f0e5a97bf38e440b",
                manifest,
            )
            self.assertTrue((share_dir / "runtime_pins.env").is_file())


if __name__ == "__main__":
    unittest.main()
