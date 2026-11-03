"""Regression tests for hash-locked CUDA sidecar candidate packaging."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import package_cuda_sm120_candidate as packager  # noqa: E402


LICENSE = b"fixture NVIDIA license notice\n"
SOURCE_LICENSE = b"fixture llama MIT license\n"


class PackageCudaSm120Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.producer_identity = {
            "schema": "airunner-native-producer-recipe-v1",
            "repository": "Capsize-Games/airunner-native",
            "profile": "cuda-sm120a", "commit": "c" * 40,
            "tree": "d" * 40, "recipes_sha256": {},
        }
        identity_check = patch.object(
            packager, "verify_identity", return_value=self.producer_identity
        )
        identity_check.start()
        self.addCleanup(identity_check.stop)
        self.build = self.base / "build-output"
        self.build.mkdir()
        (self.build / ".airunner-native-candidate").write_text(
            "airunner-native-candidate-v1:cuda-sm120\n"
        )
        server_bytes = b"fixture server ELF bytes"
        (self.build / "llama-server").write_bytes(server_bytes)
        self.server_sha = hashlib.sha256(server_bytes).hexdigest()
        (self.build / "llama-server.sha256").write_text(f"{self.server_sha}  /out/llama-server\n")
        (self.build / "LICENSE-llama.cpp.txt").write_bytes(SOURCE_LICENSE)
        (self.build / "candidate-status.env").write_text(
            "status=running\nstatus=success\n"
            f"server_sha256={self.server_sha}\n"
        )
        self.commit = "7acb4e8cd2ce21f457d1298e75fad729520d263c"
        self.tree = "8" * 40
        image = "nvidia/cuda:12.9.2-devel-ubuntu24.04@sha256:" + "1" * 64
        self.profile = self.base / "candidate.env"
        self.profile.write_text(
            f"LLAMA_CPP_COMMIT={self.commit}\nLLAMA_CPP_TREE={self.tree}\nLLAMA_CPP_VERSION=b9523\n"
            f"CUDA_IMAGE={image}\nCUDA_ARCHITECTURES=120a-real\n"
            "CMAKE_VERSION=3.31.6\nCMAKE_ARCHIVE_SHA256=" + "2" * 64 + "\n"
            "CMAKE_BINARY_SHA256=" + "3" * 64 + "\n"
        )
        self.image = image
        (self.build / "candidate-provenance.env").write_text(
            f"source_commit={self.commit}\nsource_tree={self.tree}\nsource_version=b9523\nsource_build_number=9523\n"
            f"cuda_image={image}\ncuda_architectures=120a-real\ncmake_version=3.31.6\n"
            "cmake_archive_sha256=" + "2" * 64 + "\n"
            "cmake_binary_sha256=" + "3" * 64 + "\n"
            f"version: 9523 ({self.commit})\nbuilt with GNU 13.3.0 for Linux x86_64\n"
        )
        (self.build / "build.log").write_text("fixture offline build log\n")
        (self.build / "cuda-compile-flags.make").write_text(
            "CUDA_FLAGS = --generate-code=arch=compute_120a,code=[sm_120a]\n"
        )
        (self.build / "elf-cuda-inspection.txt").write_text(
            "\n".join(f"member.sm_120a.cubin {i}" for i in range(138)) + "\n"
        )
        (self.build / "cuda-build-config.txt").write_text(
            f"source_commit={self.commit}\nCUDA enabled=ON\n"
        )
        self.receipt = self.base / "build-receipt.json"
        self.receipt.write_text(json.dumps({
            "container_id": "a" * 64,
            "image": image,
            "runtime": "runc",
            "network": "none",
            "gpu_device_requests": None,
            "cpu_limit": 2,
            "memory_limit": "8g",
            "command_constraints": ["--platform=linux/amd64", "--pull=never", "--runtime=runc",
                                    "--network=none", "--cpus=2", "--memory=8g", "--pids-limit=1024",
                                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only"],
            "result": "exit 0; candidate checks passed",
            "server_sha256": self.server_sha,
        }))
        self.notice = self.base / "gcc-copyright.txt"
        self.notice.write_bytes(b"fixture GCC runtime notice\n")
        self.platform_packages = self.base / "platform-packages.txt"
        self.platform_packages.write_bytes(b"libc6 2.39\nlibgomp1 14\nlibstdc++6 14\n")
        self.wheels_dir = self.base / "wheels"
        self.wheels_dir.mkdir()
        self.wheel_paths: dict[str, Path] = {}
        self.lock = self._make_lock()
        self.lock_path = self.base / "package-lock.json"
        self.lock_path.write_text(json.dumps(self.lock))

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _make_lock(self) -> dict:
        packages = {
            "nvidia-cuda-runtime-cu12": [
                ("nvidia/cuda_runtime/lib/libcudart.so.12", "lib/libcudart.so.12", b"cudart"),
            ],
            "nvidia-cublas-cu12": [
                ("nvidia/cublas/lib/libcublas.so.12", "lib/libcublas.so.12", b"cublas"),
                ("nvidia/cublas/lib/libcublasLt.so.12", "lib/libcublasLt.so.12", b"cublasLt"),
            ],
            "nvidia-cuda-nvrtc-cu12": [
                ("nvidia/cuda_nvrtc/lib/libnvrtc.so.12", "lib/libnvrtc.so.12", b"nvrtc"),
                ("nvidia/cuda_nvrtc/lib/libnvrtc-builtins.so.12.9", "lib/libnvrtc-builtins.so.12.9", b"builtins"),
            ],
        }
        wheels = {}
        for index, (package, members) in enumerate(packages.items()):
            filename = f"{package}-fixture.whl"
            path = self.wheels_dir / filename
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                member_rows = []
                for member, destination, data in members:
                    archive.writestr(member, data)
                    member_rows.append({
                        "member": member,
                        "destination": destination,
                        "size_bytes": len(data),
                        "sha256": self._sha(data),
                    })
                license_member = f"{package}.dist-info/licenses/License.txt"
                archive.writestr(license_member, LICENSE)
            self.wheel_paths[package] = path
            wheels[package] = {
                "filename": filename,
                "version": "fixture",
                "sha256": self._sha(path.read_bytes()),
                "size_bytes": path.stat().st_size,
                "license_member": license_member,
                "members": member_rows,
            }
        self.wheel_paths = {k: self.wheels_dir / v["filename"] for k, v in wheels.items()}
        platform_sha = self._sha(self.platform_packages.read_bytes())
        notice_sha = self._sha(self.notice.read_bytes())
        source_license_sha = self._sha(SOURCE_LICENSE)
        expected_runpaths = {
            "bin/llama-server": "$ORIGIN/../lib",
            "lib/libcublas.so.12": "$ORIGIN",
            "lib/libcublasLt.so.12": "$ORIGIN",
            "lib/libcudart.so.12": None,
            "lib/libnvrtc.so.12": "$ORIGIN:",
            "lib/libnvrtc-builtins.so.12.9": None,
        }
        needed = {
            rel: [f"needed:{Path(rel).name}"] for rel in expected_runpaths
        }
        return {
            "schema_version": 1,
            "target": "Linux x86_64 fixture",
            "source": {"repository": "https://example.invalid/llama.cpp", "commit": self.commit, "tree": self.tree,
                       "version": "b9523", "build_number": 9523, "patches_applied": []},
            "build": {"image": self.image, "cmake_version": "3.31.6",
                      "cmake_archive_sha256": "2" * 64, "cmake_binary_sha256": "3" * 64,
                      "cuda_architectures": "120a-real",
                      "expected_cubins": {"sm_120a": 138, "other_architectures": 0},
                      "expected_runpaths": expected_runpaths,
                      "expected_needed": needed},
            "reviewed_wheel_lock": {"v5_selected_artifacts_lock_sha256": "4" * 64,
                                     "static_wheel_review_lock_sha256": "5" * 64,
                                     "license_expression": "LicenseRef-NVIDIA-Proprietary",
                                     "license_member_sha256": self._sha(LICENSE),
                                     "license_member_size_bytes": len(LICENSE),
                                     "wheels": wheels},
            "expected_source_license_sha256": source_license_sha,
            "gcc_runtime": {"package": "libgomp1", "version": "14", "library_sha256": "6" * 64,
                            "copyright_notice_sha256": notice_sha, "packages_file_sha256": platform_sha},
            "reviewed_dynamic_loads": [
                {"object": "lib/libcublasLt.so.12", "soname": "libnvrtc.so.12", "evidence": "fixture"},
                {"object": "lib/libnvrtc.so.12", "soname": "libnvrtc-builtins.so.12.9", "evidence": "fixture"},
            ],
            "image_library_comparison": {"fixture": True},
        }

    def _package(self, name: str) -> tuple[Path, Path, str]:
        output = self.base / f"package-{name}"
        archive = self.base / f"candidate-{name}.tar.gz"
        with patch.object(packager, "LOCK_PATH", self.lock_path), patch.object(
            packager, "PROFILE_PATH", self.profile
        ), patch.object(packager, "readelf_dynamic", side_effect=self.fake_readelf):
            digest = packager.package_candidate(
                build_output=self.build,
                build_receipt_path=self.receipt,
                runtime_wheel=self.wheel_paths["nvidia-cuda-runtime-cu12"],
                cublas_wheel=self.wheel_paths["nvidia-cublas-cu12"],
                nvrtc_wheel=self.wheel_paths["nvidia-cuda-nvrtc-cu12"],
                gcc_notice=self.notice,
                platform_packages=self.platform_packages,
                output_dir=output,
                archive_path=archive,
            )
        return output, archive, digest

    def fake_readelf(self, path: Path) -> tuple[set[str], str | None]:
        if path.name == "llama-server":
            rel = "bin/llama-server"
        elif path.name == "libcublas.so.12":
            rel = "lib/libcublas.so.12"
        elif path.name == "libcublasLt.so.12":
            rel = "lib/libcublasLt.so.12"
        elif path.name == "libcudart.so.12":
            rel = "lib/libcudart.so.12"
        elif path.name == "libnvrtc.so.12":
            rel = "lib/libnvrtc.so.12"
        elif path.name == "libnvrtc-builtins.so.12.9":
            rel = "lib/libnvrtc-builtins.so.12.9"
        else:
            raise AssertionError(f"unexpected ELF path: {path}")
        return set(self.lock["build"]["expected_needed"][rel]), self.lock["build"]["expected_runpaths"][rel]

    def test_changed_producer_prevents_packaging(self) -> None:
        from native_recipe_identity import IdentityError
        with patch.object(packager, "verify_identity", side_effect=IdentityError("recipe changed")):
            with self.assertRaisesRegex(IdentityError, "recipe changed"):
                self._package("bad-producer")
        self.assertFalse((self.base / "package-bad-producer").exists())
        self.assertFalse((self.base / "candidate-bad-producer.tar.gz").exists())

    def test_bad_wheel_hash_rejects_before_extraction(self) -> None:
        wheel = self.wheel_paths["nvidia-cuda-runtime-cu12"]
        damaged = self.base / "damaged.whl"
        damaged.write_bytes(wheel.read_bytes()[:-1] + bytes([wheel.read_bytes()[-1] ^ 1]))
        destination = self.base / "outside" / "libcudart.so.12"
        with self.assertRaisesRegex(packager.PackageError, "SHA-256"):
            packager.extract_locked_wheel(
                damaged,
                self.lock["reviewed_wheel_lock"]["wheels"]["nvidia-cuda-runtime-cu12"],
                self.lock["reviewed_wheel_lock"]["license_member_sha256"],
                len(LICENSE),
                self.base,
                "outside/LICENSE.txt",
            )
        self.assertFalse(destination.exists())

    def test_traversal_member_rejected_before_extraction(self) -> None:
        hostile = self.base / "hostile.whl"
        with zipfile.ZipFile(hostile, "w") as archive:
            archive.writestr("../escape", b"bad")
            archive.writestr("nvidia/cuda_runtime/lib/libcudart.so.12", b"cudart")
            archive.writestr("nvidia-cuda-runtime-cu12.dist-info/licenses/License.txt", LICENSE)
        item = dict(self.lock["reviewed_wheel_lock"]["wheels"]["nvidia-cuda-runtime-cu12"])
        item["filename"] = hostile.name
        item["size_bytes"] = hostile.stat().st_size
        item["sha256"] = self._sha(hostile.read_bytes())
        destination = self.base / "destination"
        with self.assertRaisesRegex(packager.PackageError, "unsafe member path"):
            packager.extract_locked_wheel(
                hostile, item, self._sha(LICENSE), len(LICENSE), destination, "licenses/License.txt"
            )
        self.assertFalse((destination / "lib/libcudart.so.12").exists())
        self.assertFalse((self.base / "escape").exists())

    def test_symlink_member_rejected(self) -> None:
        hostile = self.base / "symlink.whl"
        with zipfile.ZipFile(hostile, "w") as archive:
            info = zipfile.ZipInfo("nvidia/cuda_runtime/lib/libcudart.so.12")
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, b"../../escape")
        with zipfile.ZipFile(hostile, "r") as archive:
            with self.assertRaisesRegex(packager.PackageError, "non-regular member"):
                packager.validate_zip_names(archive, "fixture wheel")

    def test_target_platform_guard(self) -> None:
        with patch.object(packager.sys, "platform", "darwin"):
            with self.assertRaisesRegex(packager.PackageError, "Linux x86_64"):
                packager.validate_platform()

    def test_package_is_complete_and_deterministic(self) -> None:
        first, first_archive, first_hash = self._package("one")
        second, second_archive, second_hash = self._package("two")
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        self.assertEqual(first_archive.stat().st_mode & 0o777, 0o644)
        self.assertEqual(first.stat().st_mode & 0o777, 0o755)
        for relative in (
            "bin/llama-server", "bin/llama-server-cuda", "lib/libcudart.so.12",
            "lib/libcublas.so.12", "lib/libcublasLt.so.12", "lib/libnvrtc.so.12",
            "lib/libnvrtc-builtins.so.12.9", "licenses/LICENSE-llama.cpp.txt",
            "licenses/LICENSE-nvidia-cublas-cu12.txt", "licenses/LICENSE-nvidia-cuda-runtime-cu12.txt",
            "licenses/LICENSE-nvidia-cuda-nvrtc-cu12.txt", "licenses/GCC-libgomp-copyright.txt",
            "provenance/manifest.json", "provenance/file-manifest.json", "provenance/build-container-receipt.json",
            "provenance/recipe/package_cuda_sm120_candidate.py", "SHA256SUMS",
        ):
            self.assertTrue((first / relative).is_file(), relative)
        manifest = json.loads((first / "provenance/manifest.json").read_text())
        self.assertEqual(manifest["source"]["commit"], self.commit)
        self.assertEqual(manifest["producer"], self.producer_identity)
        self.assertEqual(
            json.loads((first / "provenance/producer-recipe.json").read_text()),
            self.producer_identity,
        )
        self.assertEqual(manifest["source"]["tree"], self.tree)
        self.assertEqual(manifest["build"]["server_sha256"], self.server_sha)
        self.assertFalse(list(first.rglob("libcuda.so*")))
        self.assertEqual((first / "bin/llama-server-cuda").stat().st_mode & 0o777, 0o755)

    def test_output_may_not_overlap_cpu_bundle_path(self) -> None:
        cpu_root = self.base / "cpu-runtime-sidecars"
        with patch.object(packager, "CPU_OUTPUT_ROOT", cpu_root), patch.object(
            packager, "LOCK_PATH", self.lock_path
        ), patch.object(
            packager, "PROFILE_PATH", self.profile
        ), patch.object(packager, "readelf_dynamic", side_effect=self.fake_readelf):
            with self.assertRaisesRegex(packager.PackageError, "protected path"):
                packager.package_candidate(
                    build_output=self.build,
                    build_receipt_path=self.receipt,
                    runtime_wheel=self.wheel_paths["nvidia-cuda-runtime-cu12"],
                    cublas_wheel=self.wheel_paths["nvidia-cublas-cu12"],
                    nvrtc_wheel=self.wheel_paths["nvidia-cuda-nvrtc-cu12"],
                    gcc_notice=self.notice,
                    platform_packages=self.platform_packages,
                    output_dir=cpu_root / "candidate-test",
                    archive_path=self.base / "cpu-path-rejected.tar.gz",
                )
        self.assertFalse(cpu_root.exists())

    def test_failed_build_receipt_never_stages_an_archive(self) -> None:
        (self.build / "candidate-status.env").write_text("status=failed\nserver_sha256=bad\n")
        output = self.base / "must-not-exist"
        archive = self.base / "must-not-exist.tar.gz"
        with patch.object(packager, "LOCK_PATH", self.lock_path), patch.object(
            packager, "PROFILE_PATH", self.profile
        ):
            with self.assertRaisesRegex(packager.PackageError, "did not finish successfully"):
                packager.package_candidate(
                    build_output=self.build,
                    build_receipt_path=self.receipt,
                    runtime_wheel=self.wheel_paths["nvidia-cuda-runtime-cu12"],
                    cublas_wheel=self.wheel_paths["nvidia-cublas-cu12"],
                    nvrtc_wheel=self.wheel_paths["nvidia-cuda-nvrtc-cu12"],
                    gcc_notice=self.notice,
                    platform_packages=self.platform_packages,
                    output_dir=output,
                    archive_path=archive,
                )
        self.assertFalse(output.exists())
        self.assertFalse(archive.exists())

    def test_source_tree_identity_is_bound_to_build_provenance(self) -> None:
        provenance_path = self.build / "candidate-provenance.env"
        provenance_path.write_text(
            provenance_path.read_text(encoding="utf-8").replace(self.tree, "9" * 40),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packager.PackageError, "wrong source_tree"):
            packager.validate_build_inputs(self.build, self.receipt, self.lock)

    def test_build_receipt_must_record_networkless_resource_limits(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["command_constraints"].remove("--network=none")
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(packager.PackageError, "omits required isolation"):
            packager.validate_build_inputs(self.build, self.receipt, self.lock)


if __name__ == "__main__":
    unittest.main()
