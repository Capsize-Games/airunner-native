"""Offline tests for deterministic sidecar packaging and release preflight."""

from __future__ import annotations

import hashlib
import json
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
import hashlib
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
def save(): state_path.write_text(json.dumps(state))
def log(value):
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as stream:
        stream.write(value + "\\n")
if args[0] == "api":
    route = args[1]
    method = args[args.index("--method") + 1] if "--method" in args else "GET"
    if "/git/ref/tags/" in route:
        value = {"object": {"sha": os.environ["FAKE_REMOTE_TAG_OBJECT"], "type": "tag"}}
        if "--jq" in args: print(value["object"]["sha"] + "\\ttag")
        else: print(json.dumps(value))
    elif "/git/tags/" in route:
        value = {"object": {"sha": os.environ["FAKE_REMOTE_SOURCE"], "type": "commit"}}
        if "--jq" in args: print(value["object"]["sha"] + "\\tcommit")
        else: print(json.dumps(value))
    elif "/releases/tags/" in route:
        if state["release"] is None: raise SystemExit(1)
        print(json.dumps(state["release"]))
    elif route.endswith("/assets?per_page=100"):
        print(json.dumps(state["assets"]))
    elif route.endswith("/releases/71") and method == "PATCH":
        state["release"]["draft"] = False
        state["release"]["immutable"] = os.environ.get("FAKE_PUBLISH_IMMUTABLE", "true") == "true"
        save(); log("PATCH " + route); print(json.dumps(state["release"]))
    else:
        raise SystemExit("unexpected gh api route: " + route)
elif args[0] == "release" and args[1] == "create":
    tag = args[2]
    state["release"] = {"id": 71, "tag_name": tag, "draft": True, "prerelease": False, "immutable": False}
    save(); log("CREATE " + tag)
elif args[0] == "release" and args[1] == "upload":
    tag = args[2]
    if state["release"] is None or not state["release"]["draft"]: raise SystemExit("not a draft")
    for item in args[3:]:
        if item == "--repo": break
        name = Path(item).name
        fail_name = os.environ.get("FAKE_FAIL_UPLOAD_NAME", "")
        if name == fail_name and not state.get("failed_once"):
            state["failed_once"] = True
            save(); raise SystemExit("synthetic upload failure")
        payload = Path(item).read_bytes()
        state["assets"].append({"id": len(state["assets"]) + 1, "name": name,
            "state": "uploaded", "digest": "sha256:" + hashlib.sha256(payload).hexdigest()})
    save(); log("UPLOAD " + tag + " " + " ".join(Path(p).name for p in args[3:] if p != "--repo"))
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
                   release_tag: str = "v0.2.0", remote_tag_object: str | None = None,
                   event_ref: str = "refs/tags/v0.2.0", mode: str = "promote",
                   publish_immutable: str = "true", fail_upload_name: str = "",
                   initial_release: dict | None = None, initial_assets: list | None = None):
        repo, source, tag_object = self.make_git_repo(root, annotated=annotated)
        bin_dir = root / "fake-bin"
        bin_dir.mkdir()
        self.make_fake_gh(bin_dir)
        assets = []
        for platform in ("linux", "windows"):
            archive = root / f"runtime-sidecars-{platform}.tar.gz"
            archive.write_bytes(f"{platform} fixture archive".encode())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum = root / f"{archive.name}.sha256"
            checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            provenance = root / f"runtime-sidecars-{platform}-provenance.txt"
            provenance.write_text(
                f"source_repository=Capsize-Games/airunner-native\n"
                f"source_sha={source}\nsource_ref={event_ref}\ntarget_platform={platform}\n",
                encoding="utf-8",
            )
            assets.extend((archive, checksum, provenance))
        log = root / "gh.log"
        state_path = root / "gh-state.json"
        state_path.write_text(json.dumps({
            "release": initial_release,
            "assets": initial_assets or [],
        }), encoding="utf-8")
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
            "FAKE_GH_LOG": str(log),
            "FAKE_STATE": str(state_path),
            "FAKE_PUBLISH_IMMUTABLE": publish_immutable,
            "FAKE_FAIL_UPLOAD_NAME": fail_upload_name,
        })
        if initial_release is not None and release_tag != "v0.2.0":
            initial_release["tag_name"] = release_tag
            state_path.write_text(json.dumps({"release": initial_release, "assets": initial_assets or []}), encoding="utf-8")
        if mode == "check-tag":
            arguments = ["--check-tag", "v0.2.0"]
        elif mode == "verify-published-tag":
            arguments = ["--verify-published-tag", "v0.2.0"]
        else:
            arguments = ["v0.2.0", *map(str, assets)]
        result = subprocess.run(
            ["bash", str(UPLOAD_SCRIPT), *arguments],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, log, state_path, repo, source, env, assets

    def test_pull_request_tests_are_separate_from_native_build_workflow(self) -> None:
        build_workflow = (REPO / ".github/workflows/native-runtime-sidecars.yml").read_text(encoding="utf-8")
        test_workflow = (REPO / ".github/workflows/native-sidecar-tests.yml").read_text(encoding="utf-8")
        self.assertNotIn("pull_request:", build_workflow)
        self.assertIn("pull_request:", test_workflow)
        self.assertIn("if: github.event_name == 'release'", build_workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", build_workflow)
        self.assertIn("verify-published-release", build_workflow)
        self.assertIn("publish_release", build_workflow)
        self.assertNotIn("gh release upload", build_workflow)
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

    def test_promotion_creates_draft_uploads_all_assets_and_publishes_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, state_path, _repo, _source, _env, _assets = self.run_upload(Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            uploaded = log.read_text(encoding="utf-8")
            self.assertLess(uploaded.index("CREATE v0.2.0"), uploaded.index("UPLOAD v0.2.0"))
            self.assertLess(uploaded.index("UPLOAD v0.2.0"), uploaded.index("PATCH repos/"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["release"]["draft"])
            self.assertTrue(state["release"]["immutable"])
            self.assertEqual(len(state["assets"]), 6)

    def test_upload_rejects_lightweight_tag_and_mismatched_remote_peel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, *_ = self.run_upload(Path(temporary), annotated=False)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())
        with tempfile.TemporaryDirectory() as temporary:
            other = "2" * 40
            result, log, *_ = self.run_upload(Path(temporary), remote_source=other)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())

    def test_upload_refuses_existing_changed_asset_before_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            existing = {"id": 71, "tag_name": "v0.2.0", "draft": True, "prerelease": False, "immutable": False}
            assets = [{"id": 1, "name": "runtime-sidecars-linux.tar.gz.sha256", "state": "uploaded", "digest": "sha256:" + "0" * 64}]
            result, log, _state, *_ = self.run_upload(Path(temporary), initial_release=existing, initial_assets=assets)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("unexpected or changed asset", result.stderr)
            self.assertNotIn("UPLOAD", log.read_text(encoding="utf-8") if log.exists() else "")

    def test_upload_rejects_non_tag_event_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, *_ = self.run_upload(Path(temporary), event_ref="refs/heads/main")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Invalid source SHA, repository, or version tag", result.stderr)
            self.assertFalse(log.exists())

    def test_upload_rejects_remote_tag_object_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, *_ = self.run_upload(Path(temporary), remote_tag_object="3" * 40)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("same annotated tag object", result.stderr)
            self.assertFalse(log.exists())

    def test_promotion_resumes_partial_draft_after_upload_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, state_path, repo, _source, env, assets = self.run_upload(
                Path(temporary), fail_upload_name="runtime-sidecars-windows.tar.gz"
            )
            self.assertNotEqual(result.returncode, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["release"]["draft"])
            self.assertEqual(len(state["assets"]), 4)
            retry = subprocess.run(
                ["bash", str(UPLOAD_SCRIPT), "v0.2.0", *map(str, assets)],
                cwd=repo, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["release"]["draft"])
            self.assertEqual(len(state["assets"]), 6)
            self.assertEqual(log.read_text(encoding="utf-8").count("CREATE v0.2.0"), 1)

    def test_published_event_verification_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, source, _tag_object = self.make_git_repo(root)
            assets = []
            for platform in ("linux", "windows"):
                archive = root / f"runtime-sidecars-{platform}.tar.gz"
                archive.write_bytes(f"{platform} fixture archive".encode())
                checksum = root / f"{archive.name}.sha256"
                checksum.write_text(
                    f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
                    encoding="utf-8",
                )
                provenance = root / f"runtime-sidecars-{platform}-provenance.txt"
                provenance.write_text("fixture\n", encoding="utf-8")
                assets.extend((archive, checksum, provenance))
            bin_dir = root / "fake-bin"; bin_dir.mkdir(); self.make_fake_gh(bin_dir)
            state_path = root / "gh-state.json"
            api_assets = [
                {"id": index + 1, "name": path.name, "state": "uploaded",
                 "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                for index, path in enumerate(assets)
            ]
            state_path.write_text(json.dumps({
                "release": {"id": 71, "tag_name": "v0.2.0", "draft": False,
                            "prerelease": False, "immutable": True},
                "assets": api_assets,
            }), encoding="utf-8")
            log = root / "gh.log"
            env = os.environ.copy(); env.update({
                "PATH": f"{bin_dir}:{env['PATH']}", "GH_TOKEN": "fixture-token",
                "GITHUB_REPOSITORY": "Capsize-Games/airunner-native", "GITHUB_SHA": source,
                "GITHUB_REF": "refs/tags/v0.2.0", "FAKE_REMOTE_TAG_OBJECT": _tag_object,
                "FAKE_REMOTE_SOURCE": source, "FAKE_GH_LOG": str(log), "FAKE_STATE": str(state_path),
            })
            result = subprocess.run(
                ["bash", str(UPLOAD_SCRIPT), "--verify-published-tag", "v0.2.0"],
                cwd=repo, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(log.exists())

    def test_promotion_fails_when_published_release_is_not_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, state_path, *_ = self.run_upload(Path(temporary), publish_immutable="false")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("immutable stable release", result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["release"]["draft"])
            self.assertFalse(state["release"]["immutable"])
            self.assertIn("PATCH", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
