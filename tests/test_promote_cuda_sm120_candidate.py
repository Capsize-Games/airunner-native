"""Stdlib-only tests for explicit CUDA candidate promotion evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import promote_cuda_sm120_candidate as promotion  # noqa: E402


class PromoteCudaCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self._make_producer_source()
        self._commit("candidate producer source")
        self.producer_commit = self.git("rev-parse", "HEAD").strip()
        self.producer_tree = self.git("rev-parse", "HEAD^{tree}").strip()
        self.producer = self._producer_receipt()
        self._copy_promotion_tool()
        (self.repo / "release-note.txt").write_text("promotion source\n")
        self._commit("promotion source")
        self.promotion_commit = self.git("rev-parse", "HEAD").strip()
        self.promotion_tag = "v-fixture"
        self.git("tag", "-a", self.promotion_tag, "-m", "fixture release")
        lock = json.loads(promotion.LOCK_PATH.read_text())
        lock["expected_source_license_sha256"] = hashlib.sha256(
            b"llama license fixture\n"
        ).hexdigest()
        self.lock = json.dumps(lock).encode()

        self.package = self.base / "candidate-tree" / promotion.ARCHIVE_ROOT_NAME
        self.package.mkdir(parents=True)
        self.server_bytes = b"fixture CUDA ELF, never executed"
        self.server_sha = hashlib.sha256(self.server_bytes).hexdigest()
        self._make_candidate_tree()
        self.candidate_archive = self.base / "candidate.tar.gz"
        self._write_candidate_archive()
        self.archive_sha = hashlib.sha256(self.candidate_archive.read_bytes()).hexdigest()
        self.checksum = Path(str(self.candidate_archive) + ".sha256")
        self.checksum.write_text(f"{self.archive_sha}  {self.candidate_archive.name}\n")
        self.acceptance_path = self.base / "acceptance.json"
        self.acceptance = self._acceptance()
        self.acceptance_path.write_text(json.dumps(self.acceptance))
        self.expected_model_sha = "b58fe056b5435070240de259f3f981aa38fee96825bbd78c088d5fd90e46f2b5"
        self.output = self.base / "promoted"

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE
        ).decode().strip()

    def _commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", message)

    def _make_producer_source(self) -> None:
        paths = (
            "scripts/native_recipe_identity.py",
            "scripts/build_cuda_sm120_candidate.sh",
            "scripts/package_cuda_sm120_candidate.py",
            "scripts/fetch_cuda_sm120_inputs.py",
            "runtime_sidecars/cuda-sm120-candidate.env",
            "runtime_sidecars/cuda-sm120-package-lock.json",
            "runtime_sidecars/patches/llama.cpp-cuda-all-mtp-offload.patch",
            "runtime_sidecars/licenses/GCC-libgomp-copyright.txt",
            "runtime_sidecars/ubuntu-24.04-platform-runtime-packages.txt",
        )
        identity = (
            "PROFILES = {'cuda-sm120a': (\n" +
            "".join(f"    {path!r},\n" for path in paths) +
            ")}\n"
        )
        for relative in paths:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(identity if relative.endswith("native_recipe_identity.py") else f"fixture {relative}\n")
        # Add the exact promotion program to the later tagged source identity.
        tool = self.repo / "scripts/promote_cuda_sm120_candidate.py"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_bytes(Path(promotion.__file__).read_bytes())

    def _copy_promotion_tool(self) -> None:
        # The tool was included in the producer commit so its tagged bytes are testable.
        pass

    def _producer_receipt(self) -> dict:
        identity = json.loads(json.dumps({
            "schema": "airunner-native-producer-recipe-v1",
            "repository": promotion.REPOSITORY,
            "profile": "cuda-sm120a",
            "commit": self.producer_commit,
            "tree": self.producer_tree,
            "recipes_sha256": {},
        }))
        for relative in promotion._recipe_paths_from_commit(self.repo, self.producer_commit):
            data = subprocess.check_output(
                ["git", "-C", str(self.repo), "show", f"{self.producer_commit}:{relative}"]
            )
            identity["recipes_sha256"][relative] = hashlib.sha256(data).hexdigest()
        return identity

    def _make_candidate_tree(self) -> None:
        lock = json.loads(self.lock)
        files = {
            "bin/llama-server": (self.server_bytes, 0o755),
            "bin/llama-server-cuda": (
                b'#!/bin/sh\nserver_dir="$(cd -- "$(dirname -- "$0")" && pwd)"\n'
                b'lib_dir=$(CDPATH= cd -- "$server_dir/../lib" && pwd -P)\n'
                b'LD_LIBRARY_PATH=$lib_dir\nexport LD_LIBRARY_PATH\n'
                b'cd /\nexec "$server_dir/llama-server" "$@"\n', 0o755,
            ),
            "lib/libfixture.so": (b"fixture runtime library", 0o644),
            "licenses/LICENSE-llama.cpp.txt": (b"llama license fixture\n", 0o644),
            "provenance/platform-runtime-packages.txt": (b"fixture packages\n", 0o644),
        }
        for relative, (data, mode) in files.items():
            path = self.package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        source = dict(lock["source"])
        source["repository"] = source["repository"]
        self.candidate_manifest = {
            "schema": "airunner-native-cuda-sidecar-candidate-v1",
            "patches_applied": lock["source"]["patches_applied"],
            "artifact": "fixture candidate",
            "status": "candidate only; not published, activated, or GPU-runtime tested",
            "source": source,
            "producer": self.producer,
            "build": {
                **lock["build"],
                "server_sha256": self.server_sha,
                "runpath": "$ORIGIN/../lib",
                "launch_entrypoint": "bin/llama-server-cuda",
                "container_id": "a" * 64,
            },
        }
        self._write_json(self.package / "provenance/manifest.json", self.candidate_manifest)
        self._write_json(self.package / "provenance/producer-recipe.json", self.producer)
        self._write_json(self.package / "provenance/build-container-receipt.json", {
            "container_id": "a" * 64,
            "image": lock["build"]["image"],
            "runtime": "runc",
            "network": "none",
            "gpu_device_requests": None,
            "cpu_limit": 2,
            "memory_limit": "8g",
            "command_constraints": [
                "--platform=linux/amd64", "--pull=never", "--runtime=runc",
                "--network=none", "--cpus=2", "--memory=8g", "--pids-limit=1024",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            ],
            "result": "exit 0; candidate checks passed",
            "server_sha256": self.server_sha,
        })
        rows = []
        for path in sorted(self.package.rglob("*")):
            if path.is_file():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
                rows.append({
                    "path": path.relative_to(self.package).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
        self._write_json(self.package / "provenance/file-manifest.json", {
            "schema": "candidate-file-manifest-v1", "algorithm": "SHA-256", "files": rows,
        })
        sums = []
        for path in sorted(self.package.rglob("*")):
            if path.is_file():
                sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(self.package).as_posix()}")
        (self.package / "SHA256SUMS").write_text("\n".join(sums) + "\n")

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        path.chmod(0o644)

    def _acceptance(self) -> dict:
        return {
            "schema": "airunner-native-cuda-acceptance-v1",
            "status": "passed",
            "target_profile": "cuda-sm120a",
            "run_id": "fixture-run-001",
            "candidate_archive_sha256": self.archive_sha,
            "server_sha256": self.server_sha,
            "model_sha256": "b58fe056b5435070240de259f3f981aa38fee96825bbd78c088d5fd90e46f2b5",
            "model_context": 32768,
            "gpu_intent": True,
            "requested_gpu_layers": -1,
            "fit": "off",
            "device_observed": {
                "source": "server-log",
                "identity": "GPU-UUID-fixture",
                "compute_capability": "12.0",
                "evidence_sha256": "e" * 64,
            },
            "offloaded_layers": 80,
            "total_offloadable_layers": 80,
            "authenticated_api": True,
            "inference_passed": True,
            "cleanup_passed": True,
            "started_at_utc": "2026-11-03T00:00:00Z",
            "finished_at_utc": "2026-11-03T00:01:00Z",
        }

    def test_accepts_server_and_driver_bound_device_evidence(self) -> None:
        promotion._validate_acceptance_device({
            "source": "server-log+nvidia-smi",
            "identity": "NVIDIA GeForce RTX 5080",
            "compute_capability": "12.0",
            "evidence_sha256": "e" * 64,
        })

    def _run(self) -> tuple[Path, Path, Path, Path]:
        self.acceptance_path.write_text(json.dumps(self.acceptance))
        with patch.object(promotion, "ROOT", self.repo), patch.object(
            promotion, "LOCK_PATH", self.base / "cuda-lock.json"
        ):
            (self.base / "cuda-lock.json").write_bytes(self.lock)
            return promotion.promote_candidate(
                candidate_archive=self.candidate_archive,
                acceptance_path=self.acceptance_path,
                expected_model_sha256=self.expected_model_sha,
                output_dir=self.output,
                promotion_source_sha=self.promotion_commit,
                promotion_source_ref=f"refs/tags/{self.promotion_tag}",
            )

    def _assert_no_output(self) -> None:
        self.assertFalse(self.output.exists())

    def test_success_emits_selector_profile_and_three_release_assets(self) -> None:
        # Fixture receipt/model pin are intentionally equal only for this passing case.
        paths = self._run()
        self.assertEqual(len(paths), 4)
        profile, archive, checksum, provenance = paths
        self.assertTrue(profile.is_dir())
        manifest = json.loads((profile / "profile-manifest.json").read_text())
        self.assertEqual(manifest["schema"], promotion.PROFILE_SCHEMA)
        self.assertEqual(manifest["status"], "verified")
        self.assertEqual(manifest["profile"], "cuda-sm120a")
        self.assertEqual(manifest["source"]["commit"], self.candidate_manifest["source"]["commit"])
        self.assertEqual(manifest["producer"]["commit"], self.producer_commit)
        listed = {row["path"]: row["sha256"] for row in manifest["files"]}
        self.assertEqual(set(listed), {
            path.relative_to(profile).as_posix()
            for path in profile.rglob("*") if path.is_file() and path.name != "profile-manifest.json"
        })
        for relative, digest in listed.items():
            self.assertEqual(hashlib.sha256((profile / relative).read_bytes()).hexdigest(), digest)
        self.assertIn("--", (profile / "bin/llama-server-cuda").read_text())
        self.assertEqual(archive.name, "runtime-sidecars-linux-cuda-sm120a.tar.gz")
        self.assertEqual(checksum.read_text(), f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        text = provenance.read_text()
        for line in (
            f"source_repository={promotion.REPOSITORY}",
            f"source_sha={self.promotion_commit}",
            f"source_ref=refs/tags/{self.promotion_tag}",
            "target_platform=linux-cuda-sm120a",
            f"candidate_producer_commit={self.producer_commit}",
        ):
            self.assertIn(line, text)
        with tarfile.open(archive, "r:gz") as packed:
            self.assertTrue(any(member.name.startswith(promotion.PROFILE_ROOT_NAME + "/profile-manifest.json") for member in packed))
        second_output = self.base / "promoted-again"
        with patch.object(promotion, "ROOT", self.repo), patch.object(
            promotion, "LOCK_PATH", self.base / "cuda-lock.json"
        ):
            promotion.promote_candidate(
                candidate_archive=self.candidate_archive,
                acceptance_path=self.acceptance_path,
                expected_model_sha256=self.expected_model_sha,
                output_dir=second_output,
                promotion_source_sha=self.promotion_commit,
                promotion_source_ref=f"refs/tags/{self.promotion_tag}",
            )
        self.assertEqual(archive.read_bytes(), (second_output / archive.name).read_bytes())

    def test_rejects_unrun_false_and_mismatched_acceptance_before_output(self) -> None:
        for field, value in (
            ("status", "running"),
            ("gpu_intent", False),
            ("authenticated_api", False),
            ("inference_passed", False),
            ("candidate_archive_sha256", "0" * 64),
            ("server_sha256", "0" * 64),
            ("model_sha256", "0" * 64),
            ("model_context", 8192),
            ("requested_gpu_layers", 20),
            ("fit", "auto"),
            ("offloaded_layers", 79),
            ("total_offloadable_layers", 81),
            ("device_observed", {
                "source": "nvidia-smi", "identity": "GPU",
                "compute_capability": "12.0", "evidence_sha256": "e" * 64,
            }),
            ("finished_at_utc", "not-a-timestamp"),
            ("cleanup_passed", False),
        ):
            with self.subTest(field=field):
                self.acceptance[field] = value
                with self.assertRaises(promotion.PromotionError):
                    self._run()
                self._assert_no_output()
                self.acceptance = self._acceptance()

    def test_requires_expected_pinned_model_hash(self) -> None:
        with self.assertRaisesRegex(promotion.PromotionError, "expected model"):
            with patch.object(promotion, "ROOT", self.repo), patch.object(
                promotion, "LOCK_PATH", self.base / "cuda-lock.json"
            ):
                (self.base / "cuda-lock.json").write_bytes(self.lock)
                promotion.promote_candidate(
                    candidate_archive=self.candidate_archive,
                    acceptance_path=self.acceptance_path,
                    expected_model_sha256="bad",
                    output_dir=self.output,
                    promotion_source_sha=self.promotion_commit,
                    promotion_source_ref=f"refs/tags/{self.promotion_tag}",
                )
        self._assert_no_output()

    def test_rejects_corrupt_checksum_and_unsafe_tar_without_partial_output(self) -> None:
        self.checksum.write_text("0" * 64 + "  candidate.tar.gz\n")
        with self.assertRaisesRegex(promotion.PromotionError, "checksum"):
            self._run()
        self._assert_no_output()
        self.checksum.write_text(f"{self.archive_sha}  {self.candidate_archive.name}\n")
        self.candidate_archive.write_bytes(b"not a tar")
        self.checksum.write_text(f"{hashlib.sha256(self.candidate_archive.read_bytes()).hexdigest()}  {self.candidate_archive.name}\n")
        with self.assertRaises(promotion.PromotionError):
            self._run()
        self._assert_no_output()

    def test_rejects_symlink_path_escape_and_unlisted_directory_members(self) -> None:
        for kind in ("symlink", "path_escape", "unlisted_directory"):
            with self.subTest(kind=kind):
                self._write_candidate_archive(kind)
                self.archive_sha = hashlib.sha256(self.candidate_archive.read_bytes()).hexdigest()
                self.checksum.write_text(f"{self.archive_sha}  {self.candidate_archive.name}\n")
                self.acceptance["candidate_archive_sha256"] = self.archive_sha
                self.acceptance_path.write_text(json.dumps(self.acceptance))
                with self.assertRaises(promotion.PromotionError):
                    self._run()
                self._assert_no_output()
        self._write_candidate_archive()
        self.archive_sha = hashlib.sha256(self.candidate_archive.read_bytes()).hexdigest()
        self.checksum.write_text(f"{self.archive_sha}  {self.candidate_archive.name}\n")
        self.acceptance["candidate_archive_sha256"] = self.archive_sha
        self.acceptance_path.write_text(json.dumps(self.acceptance))

    def _write_candidate_archive(self, extra: str | None = None) -> None:
        with tarfile.open(self.candidate_archive, "w:gz") as archive:
            for path in [self.package, *sorted(self.package.rglob("*"))]:
                rel = Path(promotion.ARCHIVE_ROOT_NAME) if path == self.package else Path(promotion.ARCHIVE_ROOT_NAME) / path.relative_to(self.package)
                info = archive.gettarinfo(str(path), arcname=rel.as_posix())
                if path.is_file():
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    info.mode = 0o755
                    archive.addfile(info)
            if extra == "symlink":
                info = tarfile.TarInfo(f"{promotion.ARCHIVE_ROOT_NAME}/linked")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                archive.addfile(info)
            elif extra == "path_escape":
                info = tarfile.TarInfo("../outside")
                info.size = 1
                import io
                archive.addfile(info, io.BytesIO(b"x"))
            elif extra == "unlisted_directory":
                info = tarfile.TarInfo(f"{promotion.ARCHIVE_ROOT_NAME}/unlisted-empty-dir")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)

    def test_candidate_source_patch_must_match_package_lock(self) -> None:
        original = self.candidate_manifest["patches_applied"]
        self.candidate_manifest["patches_applied"] = []
        self._refresh_candidate_manifests()
        self._rewrite_archive_and_receipt()
        with self.assertRaisesRegex(promotion.PromotionError, "source patch identity"):
            self._run()
        self._assert_no_output()
        self.candidate_manifest["patches_applied"] = original
        self._refresh_candidate_manifests()
        self._rewrite_archive_and_receipt()

    def test_rejects_wrong_or_changed_producer_recipe_identity(self) -> None:
        bad = dict(self.producer)
        bad["recipes_sha256"] = dict(bad["recipes_sha256"])
        first = next(iter(bad["recipes_sha256"]))
        bad["recipes_sha256"][first] = "0" * 64
        self._write_json(self.package / "provenance/producer-recipe.json", bad)
        self._refresh_candidate_manifests()
        self._rewrite_archive_and_receipt()
        with self.assertRaises(promotion.PromotionError):
            self._run()
        self._assert_no_output()

    def _refresh_candidate_manifests(self) -> None:
        path = self.package / "provenance/manifest.json"
        self.candidate_manifest["producer"] = json.loads((self.package / "provenance/producer-recipe.json").read_text())
        self._write_json(path, self.candidate_manifest)
        (self.package / "provenance/file-manifest.json").unlink()
        (self.package / "SHA256SUMS").unlink()
        rows = []
        for file_path in sorted(self.package.rglob("*")):
            if file_path.is_file():
                rows.append({"path": file_path.relative_to(self.package).as_posix(),
                             "size_bytes": file_path.stat().st_size,
                             "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest()})
        self._write_json(self.package / "provenance/file-manifest.json", {
            "schema": "candidate-file-manifest-v1", "algorithm": "SHA-256", "files": rows,
        })
        sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(self.package).as_posix()}"
                for p in sorted(self.package.rglob("*")) if p.is_file()]
        (self.package / "SHA256SUMS").write_text("\n".join(sums) + "\n")

    def _rewrite_archive_and_receipt(self) -> None:
        self.candidate_archive.unlink()
        self._write_candidate_archive()
        self.archive_sha = hashlib.sha256(self.candidate_archive.read_bytes()).hexdigest()
        self.checksum.write_text(f"{self.archive_sha}  {self.candidate_archive.name}\n")
        self.acceptance["candidate_archive_sha256"] = self.archive_sha
        self.acceptance_path.write_text(json.dumps(self.acceptance))

    def test_annotated_tag_and_profile_source_are_bound_to_promotion_code(self) -> None:
        with patch.object(promotion, "ROOT", self.repo), patch.object(
            promotion, "LOCK_PATH", self.base / "cuda-lock.json"
        ):
            (self.base / "cuda-lock.json").write_bytes(self.lock)
            with self.assertRaisesRegex(promotion.PromotionError, "annotated tag"):
                promotion._validate_promotion_identity(self.promotion_commit, "refs/tags/HEAD")
            script = self.repo / "scripts/promote_cuda_sm120_candidate.py"
            script.write_text("changed bytes")
            self._commit("change promotion tool")
            changed_commit = self.git("rev-parse", "HEAD").strip()
            self.git("tag", "-a", "v-changed", "-m", "changed tool")
            with self.assertRaisesRegex(promotion.PromotionError, "tool bytes"):
                promotion._validate_promotion_identity(changed_commit, "refs/tags/v-changed")
        self._assert_no_output()

    def test_current_recipe_input_change_blocks_promotion(self) -> None:
        relative = next(iter(self.producer["recipes_sha256"]))
        target = self.repo / relative
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"changed after build\n")
            with patch.object(promotion, "ROOT", self.repo), patch.object(
                promotion, "LOCK_PATH", self.base / "cuda-lock.json"
            ):
                (self.base / "cuda-lock.json").write_bytes(self.lock)
                with self.assertRaisesRegex(promotion.PromotionError, "current compatible producer input changed"):
                    promotion._verify_producer_identity(self.repo, self.producer)
        finally:
            target.write_bytes(original)
        self._assert_no_output()

    def test_producer_verification_uses_supplied_checkout_for_current_inputs(self) -> None:
        relative = next(iter(self.producer["recipes_sha256"]))
        unrelated_root = self.base / "unrelated-global-root"
        wrong_input = unrelated_root / relative
        wrong_input.parent.mkdir(parents=True)
        wrong_input.write_bytes(b"different checkout input")

        with patch.object(promotion, "ROOT", unrelated_root):
            verified, receipt_hash = promotion._verify_producer_identity(
                self.repo, self.producer
            )

        self.assertEqual(verified, self.producer)
        self.assertEqual(
            receipt_hash,
            hashlib.sha256(
                json.dumps(self.producer, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(promotion.PromotionError, "duplicate"):
            promotion._json_object(b'{"status":"passed","status":"failed"}', "receipt")

    def test_candidate_checksum_accepts_recorded_absolute_ci_filename(self) -> None:
        self.checksum.write_text(
            f"{self.archive_sha}  /home/runner/work/_temp/cuda-sm120-archive/{self.candidate_archive.name}\n"
        )
        self.assertEqual(
            promotion._parse_checksum(self.checksum, self.candidate_archive),
            self.archive_sha,
        )


if __name__ == "__main__":
    unittest.main()
