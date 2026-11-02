"""Offline tests for deterministic sidecar packaging and release preflight."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = REPO / "scripts" / "package_runtime_sidecars.sh"
UPLOAD_SCRIPT = REPO / "scripts" / "upload_native_release_assets.sh"
SOURCE_SHA = "1" * 40


class NativeReleaseAssetTests(unittest.TestCase):
    def make_fake_gh(self, bin_dir: Path) -> Path:
        gh = bin_dir / "gh"
        gh.write_text(
            """#!/usr/bin/env python3
import os
import sys
args = sys.argv[1:]
if args[0] == "api":
    route = args[1]
    if "/git/ref/tags/" in route:
        print(os.environ["FAKE_REMOTE_TAG_OBJECT"] + "\\ttag")
    elif "/git/tags/" in route:
        print(os.environ["FAKE_REMOTE_SOURCE"] + "\\tcommit")
    else:
        raise SystemExit("unexpected gh api route: " + route)
elif args[0] == "release" and args[1] == "view":
    if "--jq" in args and args[args.index("--jq") + 1] == ".tagName":
        print(os.environ.get("FAKE_RELEASE_TAG", args[2]))
    else:
        print(os.environ.get("FAKE_EXISTING_ASSETS", ""))
elif args[0] == "release" and args[1] == "upload":
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
        log.write("\\n".join(args) + "\\n")
else:
    raise SystemExit("unexpected gh invocation: " + repr(args))
""",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return gh

    def make_git_repo(self, root: Path, *, annotated: bool = True) -> tuple[Path, str, str]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
        (repo / "source.txt").write_text("fixture source\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture source"], check=True)
        source = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        if annotated:
            subprocess.run(["git", "-C", str(repo), "tag", "-a", "v0.2.0", "-m", "fixture release"], check=True)
        else:
            subprocess.run(["git", "-C", str(repo), "tag", "v0.2.0"], check=True)
        tag_object = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "refs/tags/v0.2.0"], text=True).strip()
        return repo, source, tag_object

    def run_upload(self, root: Path, *, annotated: bool = True, remote_source: str | None = None,
                   existing_assets: str = "", release_tag: str = "v0.2.0",
                   remote_tag_object: str | None = None,
                   event_ref: str = "refs/tags/v0.2.0") -> tuple[subprocess.CompletedProcess[str], Path]:
        repo, source, tag_object = self.make_git_repo(root, annotated=annotated)
        bin_dir = root / "fake-bin"
        bin_dir.mkdir()
        self.make_fake_gh(bin_dir)
        assets = []
        for name in ("runtime-sidecars-linux.tar.gz", "runtime-sidecars-linux.tar.gz.sha256",
                     "runtime-sidecars-linux-provenance.txt"):
            path = root / name
            path.write_text("fixture asset\n", encoding="utf-8")
            assets.append(str(path))
        log = root / "gh.log"
        env = os.environ.copy()
        env.update({
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GH_TOKEN": "fixture-token",
            "GITHUB_REPOSITORY": "Capsize-Games/airunner-native",
            "GITHUB_SHA": source,
            "GITHUB_REF_NAME": "v0.2.0",
            "GITHUB_REF": event_ref,
            "FAKE_REMOTE_TAG_OBJECT": remote_tag_object or tag_object,
            "FAKE_REMOTE_SOURCE": remote_source or source,
            "FAKE_RELEASE_TAG": release_tag,
            "FAKE_EXISTING_ASSETS": existing_assets,
            "FAKE_GH_LOG": str(log),
        })
        result = subprocess.run(
            ["bash", str(UPLOAD_SCRIPT), "v0.2.0", *assets],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, log

    def test_pull_request_tests_are_separate_from_native_build_workflow(self) -> None:
        build_workflow = (REPO / ".github/workflows/native-runtime-sidecars.yml").read_text(encoding="utf-8")
        test_workflow = (REPO / ".github/workflows/native-sidecar-tests.yml").read_text(encoding="utf-8")
        self.assertNotIn("pull_request:", build_workflow)
        self.assertIn("pull_request:", test_workflow)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", test_workflow)
        self.assertNotIn("./scripts/build_runtime_sidecars.sh", test_workflow)
        self.assertNotIn("--enable-cuda", test_workflow)
        self.assertIn("bash -n \"$script\"", test_workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", test_workflow)

    def test_package_is_deterministic_and_records_source_and_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            manifest = bundle / "share" / "airunner" / "runtime_manifest.env"
            original = manifest.read_bytes() if manifest.exists() else None
            try:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(
                    "AIRUNNER_LLAMA_CPP_COMMIT=fixture-llama\n"
                    "AIRUNNER_WHISPER_CPP_COMMIT=fixture-whisper\n"
                    "AIRUNNER_CMAKE_VERSION=cmake\\ version\\ fixture\n"
                    "AIRUNNER_C_COMPILER_VERSION=cc\\ fixture\n",
                    encoding="utf-8",
                )
                (bundle / "bin").mkdir(parents=True, exist_ok=True)
                (bundle / "bin" / "fixture-server").write_bytes(b"fixture binary bytes")
                env = os.environ.copy()
                env.update({
                    "GITHUB_REPOSITORY": "Capsize-Games/airunner-native",
                    "GITHUB_SHA": SOURCE_SHA,
                    "GITHUB_REF": "refs/tags/v0.2.0",
                    "AIRUNNER_SIDECAR_RELEASE_OUTPUT_DIR": str(root / "assets"),
                    "AIRUNNER_SIDECAR_BUNDLE_DIR": str(bundle),
                })
                first = subprocess.run(["bash", str(PACKAGE_SCRIPT), "linux"], env=env, capture_output=True, text=True, check=False)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                asset_dir = root / "assets"
                archive = asset_dir / "runtime-sidecars-linux.tar.gz"
                digest_before = hashlib.sha256(archive.read_bytes()).hexdigest()
                (bundle / "bin" / "fixture-server").touch()
                second = subprocess.run(["bash", str(PACKAGE_SCRIPT), "linux"], env=env, capture_output=True, text=True, check=False)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), digest_before)
                with tarfile.open(archive, "r:gz") as packed:
                    embedded = packed.extractfile("./share/airunner/source_provenance.env")
                    self.assertIsNotNone(embedded)
                    self.assertIn(f"source_sha={SOURCE_SHA}", embedded.read().decode("utf-8"))
                checksum = (asset_dir / "runtime-sidecars-linux.tar.gz.sha256").read_text(encoding="utf-8")
                self.assertIn(digest_before, checksum)
                provenance = (asset_dir / "runtime-sidecars-linux-provenance.txt").read_text(encoding="utf-8")
                self.assertIn(f"source_sha={SOURCE_SHA}", provenance)
                self.assertIn("AIRUNNER_CMAKE_VERSION=cmake\\ version\\ fixture", provenance)
                self.assertIn("tar_version=", provenance)
                self.assertIn("gzip_version=", provenance)
                self.assertIn("not claimed byte-reproducible", provenance)
            finally:
                if original is None:
                    manifest.unlink(missing_ok=True)
                else:
                    manifest.write_bytes(original)
                # Remove only fixture files created by this test.
                (bundle / "bin" / "fixture-server").unlink(missing_ok=True)

    def test_upload_requires_same_local_remote_annotated_tag_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            uploaded = log.read_text(encoding="utf-8")
            self.assertIn("release", uploaded)
            self.assertIn("upload", uploaded)
            self.assertNotIn("--clobber", uploaded)

    def test_upload_rejects_lightweight_tag_and_mismatched_remote_peel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(Path(temporary), annotated=False)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())
        with tempfile.TemporaryDirectory() as temporary:
            other = "2" * 40
            result, log = self.run_upload(Path(temporary), remote_source=other)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())

    def test_upload_checks_all_name_collisions_before_one_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(
                Path(temporary), existing_assets="runtime-sidecars-linux.tar.gz.sha256"
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("already exists", result.stderr)
            self.assertFalse(log.exists())

    def test_upload_rejects_non_tag_event_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(Path(temporary), event_ref="refs/heads/main")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Invalid source SHA or release tag", result.stderr)
            self.assertFalse(log.exists())

    def test_upload_rejects_remote_tag_object_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(Path(temporary), remote_tag_object="3" * 40)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("same annotated tag object", result.stderr)
            self.assertFalse(log.exists())

    def test_upload_rejects_release_tag_name_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self.run_upload(Path(temporary), release_tag="other-tag")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
