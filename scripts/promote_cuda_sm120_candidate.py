#!/usr/bin/env python3
"""Promote an accepted CUDA candidate into the selector's runtime profile format.

This tool only packages already-built bytes. It never runs the native server or
loads a model. The acceptance receipt must come from the separate GPU harness.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "runtime_sidecars" / "cuda-sm120-package-lock.json"
ARCHIVE_ROOT_NAME = "airunner-native-linux-x86_64-cuda-sm120"
PROFILE_ROOT_NAME = "airunner-native-runtime-cuda-sm120a"
PROFILE_SCHEMA = "airunner-native-runtime-profile-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
CHUNK = 1024 * 1024
REPOSITORY = "Capsize-Games/airunner-native"


class PromotionError(RuntimeError):
    """Input evidence or candidate archive cannot be safely promoted."""


def _sha256_file(path: Path) -> str:
    """Hash a file without reading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular(path: Path, label: str) -> None:
    """Require an existing regular file without following a final symlink."""
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise PromotionError(f"{label} is missing or inaccessible") from error
    if not stat.S_ISREG(info.st_mode):
        raise PromotionError(f"{label} must be a regular file")


def _reject_symlink_components(path: Path) -> None:
    """Reject existing symlinks in every absolute path component."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PromotionError("path contains a symlink component")


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    """Decode bounded UTF-8 JSON and reject duplicate object keys."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject repeated keys while preserving decoded object values."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise PromotionError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PromotionError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must be a JSON object")
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    """Read a bounded JSON object from a regular file."""
    _regular(path, label)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise PromotionError(f"{label} exceeds the size limit")
    return _json_object(path.read_bytes(), label)


def _hex(value: Any, size: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None


def _parse_checksum(checksum_path: Path, archive_path: Path) -> str:
    """Verify standard adjacent sha256sum output and return its digest."""
    _regular(checksum_path, "candidate checksum")
    content = checksum_path.read_text(encoding="ascii").strip()
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)", content)
    recorded_name = match.group(2) if match else ""
    accepted_name = recorded_name == archive_path.name or (
        Path(recorded_name).is_absolute() and Path(recorded_name).name == archive_path.name
    )
    if not match or not accepted_name:
        raise PromotionError("candidate checksum file has an invalid name or format")
    digest = _sha256_file(archive_path)
    if digest != match.group(1):
        raise PromotionError("candidate archive does not match its checksum")
    return digest


def _tar_path(name: str) -> PurePosixPath:
    """Validate a canonical archive member path."""
    path = PurePosixPath(name)
    if (
        not name or "\\" in name or path.is_absolute()
        or path.as_posix() != name.rstrip("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PromotionError("candidate archive contains an unsafe member path")
    return path


def _extract_candidate(archive_path: Path, destination: Path) -> Path:
    """Safely extract the one expected candidate envelope with strict bounds."""
    root = destination / ARCHIVE_ROOT_NAME
    seen: set[str] = set()
    total = 0
    count = 0
    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as error:
        raise PromotionError("candidate archive is not a valid gzip tar") from error
    with archive:
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_FILES:
                raise PromotionError("candidate archive has too many members")
            relative = _tar_path(member.name)
            if relative.parts[0] != ARCHIVE_ROOT_NAME:
                raise PromotionError("candidate archive has an unexpected envelope")
            if member.name in seen:
                raise PromotionError("candidate archive has duplicate member paths")
            seen.add(member.name)
            if member.isreg():
                total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise PromotionError("candidate archive exceeds the uncompressed size limit")
            _extract_member(archive, member, destination, relative)
    if not root.is_dir():
        raise PromotionError("candidate archive envelope is missing")
    return root


def _extract_member(archive: tarfile.TarFile, member: tarfile.TarInfo,
                    destination: Path, relative: PurePosixPath) -> None:
    """Validate and extract one regular file or directory member."""
    mode = member.mode
    if mode & (stat.S_ISUID | stat.S_ISGID):
        raise PromotionError("candidate archive contains setuid/setgid modes")
    target = destination / Path(*relative.parts)
    if member.isdir():
        if mode != 0o755:
            raise PromotionError("candidate archive has an unsafe directory mode")
        target.mkdir(parents=True, exist_ok=True)
        return
    if not member.isreg() or member.size < 0:
        raise PromotionError("candidate archive contains a link or special file")
    if mode not in (0o644, 0o755):
        raise PromotionError("candidate archive has an unsupported file mode")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise PromotionError("candidate archive member cannot be read")
    with source, target.open("xb") as output:
        copied = 0
        while block := source.read(CHUNK):
            copied += len(block)
            if copied > member.size:
                raise PromotionError("candidate archive member exceeds declared size")
            output.write(block)
        if copied != member.size:
            raise PromotionError("candidate archive member is truncated")
    target.chmod(mode)


def _inventory(root: Path, rows: Any, *, excluded: set[str]) -> dict[str, tuple[str, int]]:
    """Validate candidate or final profile inventory against actual regular files."""
    expected = _parse_inventory_rows(rows, excluded)
    _verify_inventory_tree(root, expected, excluded)
    return expected


def _parse_inventory_rows(
    rows: Any, excluded: set[str]
) -> dict[str, tuple[str, int]]:
    """Validate and normalize inventory rows."""
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ARCHIVE_FILES:
        raise PromotionError("archive inventory is malformed")
    expected: dict[str, tuple[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PromotionError("archive inventory row is malformed")
        relative = row.get("path")
        if not isinstance(relative, str):
            raise PromotionError("archive inventory path is malformed")
        path = _tar_path(relative)
        if len(path.parts) < 1 or relative in expected:
            raise PromotionError("archive inventory path is duplicated")
        digest = row.get("sha256")
        mode = row.get("mode", None)
        size = row.get("size_bytes")
        if not _hex(digest, 64) or not isinstance(size, int) or size < 0:
            raise PromotionError("archive inventory hash or size is malformed")
        if mode is not None and mode not in (0o644, 0o755):
            raise PromotionError("archive inventory mode is unsupported")
        expected[relative] = (digest, size)
    if set(expected) & excluded:
        raise PromotionError("archive inventory lists an excluded file")
    return expected


def _verify_inventory_tree(
    root: Path, expected: dict[str, tuple[str, int]], excluded: set[str]
) -> None:
    """Match expected file names, sizes, and hashes to a filesystem tree."""
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PromotionError("extracted candidate contains a symlink")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in excluded:
                actual.add(relative)
                if relative not in expected:
                    raise PromotionError("candidate archive has an unlisted file")
                digest, size = expected[relative]
                if path.stat().st_size != size or _sha256_file(path) != digest:
                    raise PromotionError("candidate archive file differs from its inventory")
        elif not path.is_dir():
            raise PromotionError("extracted candidate contains a special file")
    if actual != set(expected):
        raise PromotionError("candidate archive inventory does not match its files")


def _verify_candidate_inventory(root: Path) -> dict[str, tuple[str, int]]:
    """Verify candidate package file manifest and SHA256SUMS closure."""
    manifest_path = root / "provenance/file-manifest.json"
    manifest = _read_json(manifest_path, "candidate file manifest")
    if manifest.get("schema") != "candidate-file-manifest-v1" or manifest.get("algorithm") != "SHA-256":
        raise PromotionError("candidate file manifest schema is unsupported")
    expected = _inventory(
        root, manifest.get("files"), excluded={"provenance/file-manifest.json", "SHA256SUMS"}
    )
    sum_rows = _parse_sha_sums(root / "SHA256SUMS")
    _verify_sha_sums(root, sum_rows)
    _verify_directory_closure(root, set(sum_rows))
    return expected


def _parse_sha_sums(sums_path: Path) -> dict[str, str]:
    """Parse the candidate's complete SHA256SUMS file."""
    _regular(sums_path, "candidate SHA256SUMS")
    sum_rows: dict[str, str] = {}
    for line in sums_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match.group(2) in sum_rows:
            raise PromotionError("candidate SHA256SUMS is malformed")
        sum_rows[match.group(2)] = match.group(1)
    return sum_rows


def _verify_sha_sums(root: Path, sum_rows: dict[str, str]) -> None:
    """Verify candidate SHA256SUMS names and bytes exactly."""
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if actual_paths != set(sum_rows):
        raise PromotionError("candidate SHA256SUMS does not cover exact files")
    for relative, digest in sum_rows.items():
        if _sha256_file(root / relative) != digest:
            raise PromotionError("candidate SHA256SUMS hash mismatch")


def _verify_directory_closure(root: Path, file_paths: set[str]) -> None:
    """Reject unlisted empty directories and omitted parent directories."""
    expected_dirs = {
        parent.as_posix()
        for relative in file_paths
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    actual_dirs = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_dirs != expected_dirs:
        raise PromotionError("candidate archive contains an unlisted directory")


def _git(root: Path, *args: str) -> bytes:
    """Read a bounded git object identity from the native source checkout."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.PIPE, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PromotionError("cannot verify promotion or producer source identity") from error


def _recipe_paths_from_commit(root: Path, commit: str) -> tuple[str, ...]:
    """Read the producer's literal recipe path table without executing source."""
    source = _git(root, "show", f"{commit}:scripts/native_recipe_identity.py").decode("utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROFILES" for target in node.targets
        ):
            profiles = ast.literal_eval(node.value)
            paths = profiles.get("cuda-sm120a")
            if isinstance(paths, tuple) and paths and all(
                isinstance(path, str) and not path.startswith("/") and ".." not in Path(path).parts
                for path in paths
            ):
                return paths
    raise PromotionError("producer recipe path table is missing or unsafe")


def _verify_producer_identity(
    root: Path, producer: Any, receipt_bytes: bytes | None = None
) -> tuple[dict[str, Any], str]:
    """Verify archived recipe hashes against source commit and current inputs."""
    if not isinstance(producer, dict) or producer.get("schema") != "airunner-native-producer-recipe-v1":
        raise PromotionError("candidate producer receipt schema is unsupported")
    commit, tree = producer.get("commit"), producer.get("tree")
    recipes = producer.get("recipes_sha256")
    if producer.get("repository") != REPOSITORY or not _hex(commit, 40) or not _hex(tree, 40):
        raise PromotionError("candidate producer source identity is malformed")
    if not isinstance(recipes, dict) or not recipes:
        raise PromotionError("candidate producer recipe hashes are missing")
    actual_tree = _git(root, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    if actual_tree != tree:
        raise PromotionError("candidate producer tree does not match its Git commit")
    paths = _recipe_paths_from_commit(root, commit)
    if set(paths) != set(recipes):
        raise PromotionError("candidate producer recipe file set differs from its committed recipe")
    for relative in paths:
        expected = recipes[relative]
        if not _hex(expected, 64):
            raise PromotionError("candidate producer recipe hash is malformed")
        committed = _git(root, "show", f"{commit}:{relative}")
        if hashlib.sha256(committed).hexdigest() != expected:
            raise PromotionError("candidate producer receipt does not match committed recipe bytes")
        current = root / relative
        if not current.is_file() or current.is_symlink() or _sha256_file(current) != expected:
            raise PromotionError(f"current compatible producer input changed: {relative}")
    if receipt_bytes is None:
        receipt_bytes = json.dumps(producer, sort_keys=True, separators=(",", ":")).encode()
    return producer, hashlib.sha256(receipt_bytes).hexdigest()


def _validate_acceptance_receipt(
    receipt: dict[str, Any], *, archive_sha: str, server_sha: str,
    expected_model_sha: str,
) -> dict[str, Any]:
    """Require exact automated GPU acceptance evidence bound to this candidate/model."""
    if receipt.get("schema") != "airunner-native-cuda-acceptance-v1" or receipt.get("status") != "passed":
        raise PromotionError("native acceptance receipt is absent, unrun, or failed")
    if receipt.get("target_profile") != "cuda-sm120a":
        raise PromotionError("native acceptance receipt targets another profile")
    _validate_acceptance_flags(receipt)
    _validate_acceptance_bindings(
        receipt, archive_sha=archive_sha, server_sha=server_sha,
        expected_model_sha=expected_model_sha,
    )
    _validate_acceptance_gpu(receipt)
    _validate_acceptance_device(receipt.get("device_observed"))
    _validate_acceptance_times(receipt)
    return receipt


def _validate_acceptance_flags(receipt: dict[str, Any]) -> None:
    """Require each acceptance check to have a literal true result."""
    flags = ("gpu_intent", "authenticated_api", "inference_passed", "cleanup_passed")
    for key in flags:
        if receipt.get(key) is not True:
            raise PromotionError(f"native acceptance receipt did not pass {key}")


def _validate_acceptance_bindings(
    receipt: dict[str, Any], *, archive_sha: str, server_sha: str,
    expected_model_sha: str,
) -> None:
    """Bind the tested archive, server, and model to promotion inputs."""
    if receipt.get("candidate_archive_sha256") != archive_sha:
        raise PromotionError("native acceptance receipt is bound to another candidate archive")
    if receipt.get("server_sha256") != server_sha:
        raise PromotionError("native acceptance receipt is bound to another server binary")
    if receipt.get("model_sha256") != expected_model_sha:
        raise PromotionError("native acceptance receipt does not match the pinned model")


def _validate_acceptance_gpu(receipt: dict[str, Any]) -> None:
    """Require a full-GPU request, exact context, and complete layer offload."""
    if receipt.get("requested_gpu_layers") != -1 or receipt.get("fit") != "off":
        raise PromotionError("native acceptance receipt did not request full GPU placement")
    if receipt.get("model_context") != 32768:
        raise PromotionError("native acceptance receipt did not run the required model context")
    offloaded = receipt.get("offloaded_layers")
    total = receipt.get("total_offloadable_layers")
    if (
        type(offloaded) is not int
        or type(total) is not int
        or total <= 0
        or offloaded != total
    ):
        raise PromotionError("native acceptance did not observe every offloadable layer on GPU")


def _validate_acceptance_device(device: Any) -> None:
    """Require device identity and capability from server-side evidence."""
    if not isinstance(device, dict) or device.get("source") not in {
        "server-log", "server-metrics", "server-log+nvidia-smi"
    }:
        raise PromotionError("native acceptance lacks server-observed device evidence")
    identity = device.get("identity")
    evidence_sha = device.get("evidence_sha256")
    if not isinstance(identity, str) or not identity.strip() or not _hex(evidence_sha, 64):
        raise PromotionError("native acceptance device identity evidence is malformed")
    if device.get("compute_capability") != "12.0":
        raise PromotionError("native acceptance did not observe a compute capability 12.0 device")


def _validate_acceptance_times(receipt: dict[str, Any]) -> None:
    """Require ordered UTC start and finish timestamps."""
    if not isinstance(receipt.get("run_id"), str) or not receipt["run_id"].strip():
        raise PromotionError("native acceptance run identifier is missing")
    if not isinstance(receipt.get("started_at_utc"), str) or not isinstance(receipt.get("finished_at_utc"), str):
        raise PromotionError("native acceptance timestamps are missing")
    try:
        started = datetime.fromisoformat(receipt["started_at_utc"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(receipt["finished_at_utc"].replace("Z", "+00:00"))
    except ValueError as error:
        raise PromotionError("native acceptance timestamps are malformed") from error
    if (
        started.tzinfo != timezone.utc
        or finished.tzinfo != timezone.utc
        or finished < started
    ):
        raise PromotionError("native acceptance timestamps are not ordered UTC values")


def _safe_copy_tree(source: Path, destination: Path) -> None:
    """Copy verified regular candidate contents without following links."""
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and not path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(stat.S_IMODE(path.stat().st_mode))
        else:
            raise PromotionError("candidate contains a symlink or special file")


def _profile_manifest(root: Path, candidate: dict[str, Any], producer: dict[str, Any], producer_hash: str) -> dict[str, Any]:
    """Create the selector-compatible strict profile manifest."""
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PromotionError("promoted profile contains a symlink")
        if path.is_file() and path.name != "profile-manifest.json":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode not in (0o644, 0o755) or mode & (stat.S_ISUID | stat.S_ISGID):
                raise PromotionError("promoted profile contains an unsafe file mode")
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256_file(path),
                "mode": mode,
            })
        elif not path.is_dir() and not path.is_file():
            raise PromotionError("promoted profile contains a special file")
    source = candidate.get("source")
    if not isinstance(source, dict):
        raise PromotionError("candidate llama.cpp source identity is missing")
    return {
        "schema": PROFILE_SCHEMA,
        "status": "verified",
        "profile": "cuda-sm120a",
        "artifact_id": f"cuda-sm120a-{producer['commit'][:12]}-{candidate['build']['server_sha256'][:12]}",
        "platform": "linux-x86_64",
        "abi": "ubuntu-24.04",
        "compute_capability": "12.0",
        "source": {
            "repository": source.get("repository"),
            "commit": source.get("commit"),
            "tree": source.get("tree"),
        },
        "producer": {"commit": producer["commit"], "recipe_sha256": producer_hash},
        "entrypoint": "bin/llama-server-cuda",
        "files": rows,
    }


def _validate_candidate_source(candidate: dict[str, Any], lock: dict[str, Any]) -> None:
    """Bind candidate source/build fields to the committed CUDA package lock."""
    source = candidate.get("source")
    build = candidate.get("build")
    expected_source = lock.get("source")
    if not isinstance(source, dict) or not isinstance(build, dict):
        raise PromotionError("candidate manifest source/build data is malformed")
    for key in ("commit", "tree", "version", "build_number"):
        if source.get(key) != expected_source.get(key):
            raise PromotionError(f"candidate source {key} differs from current package lock")
    if source.get("repository") != expected_source.get("repository"):
        raise PromotionError("candidate source repository differs from current package lock")
    expected_patches = expected_source.get("patches_applied")
    if (not isinstance(expected_patches, list) or len(expected_patches) != 1
            or candidate.get("patches_applied") != expected_patches
            or source.get("patches_applied") != expected_patches):
        raise PromotionError("candidate source patch identity differs from current package lock")
    if candidate.get("status") != "candidate only; not published, activated, or GPU-runtime tested":
        raise PromotionError("candidate provenance has an unexpected status")
    if build.get("launch_entrypoint") != "bin/llama-server-cuda":
        raise PromotionError("candidate does not use the confined CUDA wrapper")
    for key, expected in lock["build"].items():
        if build.get(key) != expected:
            raise PromotionError(f"candidate build {key} differs from current package lock")


def _validate_candidate_receipts(root: Path, candidate: dict[str, Any], lock: dict[str, Any]) -> str:
    """Recheck candidate build receipt, source notice, and confined wrapper."""
    server = root / "bin/llama-server"
    _regular(server, "candidate llama-server")
    server_sha = _sha256_file(server)
    if server_sha != candidate.get("build", {}).get("server_sha256"):
        raise PromotionError("candidate server hash differs from candidate manifest")
    source_license = root / "licenses/LICENSE-llama.cpp.txt"
    _regular(source_license, "candidate llama.cpp license")
    if _sha256_file(source_license) != lock["expected_source_license_sha256"]:
        raise PromotionError("candidate source license differs from current package lock")
    wrapper_path = root / "bin/llama-server-cuda"
    _regular(wrapper_path, "candidate CUDA wrapper")
    if wrapper_path.stat().st_size > 64 * 1024 or stat.S_IMODE(wrapper_path.stat().st_mode) != 0o755:
        raise PromotionError("candidate CUDA wrapper has unsafe size or mode")
    wrapper = wrapper_path.read_text(encoding="utf-8")
    required_fragments = (
        "$server_dir/../lib", "LD_LIBRARY_PATH=$lib_dir", "export LD_LIBRARY_PATH",
        "cd /", 'exec "$server_dir/llama-server" "$@"',
    )
    if not all(fragment in wrapper for fragment in required_fragments):
        raise PromotionError("candidate CUDA wrapper does not confine its library and working paths")

    receipt = _read_json(root / "provenance/build-container-receipt.json", "candidate build receipt")
    expected_receipt = {
        "image": lock["build"]["image"],
        "runtime": "runc",
        "network": "none",
        "gpu_device_requests": None,
        "cpu_limit": 2,
        "memory_limit": "8g",
        "server_sha256": server_sha,
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise PromotionError("candidate build receipt does not match locked isolation and binary identity")
    constraints = {
        "--platform=linux/amd64", "--pull=never", "--runtime=runc", "--network=none",
        "--cpus=2", "--memory=8g", "--pids-limit=1024", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--read-only",
    }
    actual_constraints = receipt.get("command_constraints")
    if not isinstance(actual_constraints, list) or not constraints.issubset(set(actual_constraints)):
        raise PromotionError("candidate build receipt omits required isolation controls")
    if not _hex(receipt.get("container_id"), 64) or "exit 0" not in str(receipt.get("result", "")):
        raise PromotionError("candidate build receipt lacks successful container completion")
    return server_sha


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o644)


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")
    path.chmod(0o644)


def _deterministic_archive(root: Path, archive_path: Path) -> None:
    """Write a deterministic tar.gz with one canonical component envelope."""
    with archive_path.open("xb") as raw, gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            entries = [root] + sorted(root.rglob("*"))
            for path in entries:
                relative = PurePosixPath(PROFILE_ROOT_NAME) if path == root else PurePosixPath(PROFILE_ROOT_NAME) / path.relative_to(root).as_posix()
                info = tarfile.TarInfo(relative.as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.size = 0
                    archive.addfile(info)
                elif path.is_file() and not path.is_symlink():
                    info.type = tarfile.REGTYPE
                    info.mode = stat.S_IMODE(path.stat().st_mode)
                    info.size = path.stat().st_size
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    raise PromotionError("cannot package a non-regular profile entry")


def _validate_promotion_identity(source_sha: str, source_ref: str) -> None:
    """Require this invocation to identify an annotated release source tag."""
    if (
        not _hex(source_sha, 40)
        or re.fullmatch(r"refs/tags/[A-Za-z0-9_.+-]+", source_ref) is None
    ):
        raise PromotionError("promotion source must be a full commit SHA and tag ref")
    try:
        ref_type = _git(ROOT, "cat-file", "-t", source_ref).decode().strip()
    except PromotionError as error:
        raise PromotionError("promotion source ref must be an annotated tag") from error
    if ref_type != "tag":
        raise PromotionError("promotion source ref must be an annotated tag")
    ref_sha = _git(ROOT, "rev-parse", f"{source_ref}^{{commit}}").decode().strip()
    if ref_sha != source_sha:
        raise PromotionError("promotion source tag does not resolve to the supplied commit")
    committed_tool = _git(ROOT, "show", f"{source_sha}:scripts/promote_cuda_sm120_candidate.py")
    if Path(__file__).read_bytes() != committed_tool:
        raise PromotionError("promotion tool bytes differ from the tagged source commit")


def _write_release_assets(stage: Path, profile_root: Path,
                          promotion: dict[str, Any]) -> tuple[Path, Path, Path]:
    """Package the profile and write its canonical checksum and provenance."""
    final_asset = stage / "runtime-sidecars-linux-cuda-sm120a.tar.gz"
    _deterministic_archive(profile_root, final_asset)
    archive_sha = _sha256_file(final_asset)
    checksum_out = stage / f"{final_asset.name}.sha256"
    _write_text(checksum_out, f"{archive_sha}  {final_asset.name}\n")
    provenance_out = stage / "runtime-sidecars-linux-cuda-sm120a-provenance.txt"
    source = promotion["promotion_source"]
    fields = {
        "format_version": 1,
        "source_repository": REPOSITORY,
        "source_sha": source["sha"],
        "source_ref": source["ref"],
        "target_platform": "linux-cuda-sm120a",
        "archive": final_asset.name,
        "archive_sha256": archive_sha,
        "candidate_archive_sha256": promotion["candidate_archive_sha256"],
        "candidate_producer_commit": promotion["candidate_producer_commit"],
        "candidate_producer_tree": promotion["candidate_producer_tree"],
        "candidate_llama_cpp_commit": promotion["candidate_llama_cpp_source"]["commit"],
        "server_sha256": promotion["server_sha256"],
        "model_sha256": promotion["model_sha256"],
        "acceptance_receipt_sha256": promotion["acceptance_receipt_sha256"],
    }
    _write_text(provenance_out, "".join(f"{key}={value}\n" for key, value in fields.items()))
    return final_asset, checksum_out, provenance_out


def promote_candidate(
    *, candidate_archive: Path, acceptance_path: Path, expected_model_sha256: str,
    output_dir: Path, promotion_source_sha: str, promotion_source_ref: str,
) -> tuple[Path, Path, Path, Path]:
    """Verify candidate and automated acceptance, then emit selector profile assets."""
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise PromotionError("CUDA SM120a promotion is supported only on Linux x86_64")
    if not _hex(expected_model_sha256, 64):
        raise PromotionError("expected model SHA-256 must be 64 lowercase hex characters")
    _validate_promotion_identity(promotion_source_sha, promotion_source_ref)
    _regular(candidate_archive, "candidate archive")
    if candidate_archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PromotionError("candidate archive exceeds the compressed size limit")
    checksum = Path(str(candidate_archive) + ".sha256")
    candidate_sha = _parse_checksum(checksum, candidate_archive)
    acceptance = _read_json(acceptance_path, "native acceptance receipt")
    lock = _read_json(LOCK_PATH, "CUDA package lock")
    output_dir = Path(os.path.abspath(output_dir))
    _reject_symlink_components(output_dir.parent)
    if output_dir.exists() or output_dir.is_symlink():
        raise PromotionError("refusing to overwrite promotion output directory")
    if output_dir in candidate_archive.parents or candidate_archive in output_dir.parents:
        raise PromotionError("promotion output overlaps candidate archive")
    if output_dir in acceptance_path.parents or acceptance_path in output_dir.parents:
        raise PromotionError("promotion output overlaps acceptance receipt")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        candidate_root = _extract_candidate(candidate_archive, stage / "candidate")
        _verify_candidate_inventory(candidate_root)
        candidate = _read_json(candidate_root / "provenance/manifest.json", "candidate manifest")
        _validate_candidate_source(candidate, lock)
        producer_file = candidate_root / "provenance/producer-recipe.json"
        producer = _read_json(producer_file, "producer recipe receipt")
        producer, recipe_hash = _verify_producer_identity(
            ROOT, producer, producer_file.read_bytes()
        )
        server_sha = _validate_candidate_receipts(candidate_root, candidate, lock)
        _validate_acceptance_receipt(
            acceptance, archive_sha=candidate_sha, server_sha=server_sha,
            expected_model_sha=expected_model_sha256,
        )

        profile_root = stage / PROFILE_ROOT_NAME
        profile_root.mkdir()
        _safe_copy_tree(candidate_root, profile_root)
        promotion = {
            "schema": "airunner-native-cuda-promotion-v1",
            "promotion_source": {
                "repository": REPOSITORY,
                "sha": promotion_source_sha,
                "ref": promotion_source_ref,
            },
            "candidate_archive_sha256": candidate_sha,
            "candidate_producer_commit": producer["commit"],
            "candidate_producer_tree": producer["tree"],
            "candidate_producer_recipe_sha256": recipe_hash,
            "candidate_llama_cpp_source": candidate["source"],
            "server_sha256": server_sha,
            "model_sha256": expected_model_sha256,
            "acceptance_receipt_sha256": _sha256_file(acceptance_path),
            "acceptance": acceptance,
        }
        _write_json(profile_root / "provenance/promotion.json", promotion)
        _write_text(
            profile_root / "README.md",
            "# Linux CUDA SM 120a runtime profile\n\n"
            "This runtime profile contains the verified CUDA `llama-server` binary, "
            "its packaged libraries, and the confined `bin/llama-server-cuda` entrypoint.\n\n"
            "The automated native acceptance receipt is recorded in "
            "`provenance/promotion.json`. It binds this server archive to the selected "
            "model hash, a 32,768-token context, server-observed CUDA device identity, "
            "full offload of the server-reported offloadable layers, authenticated API "
            "inference, and owned-process cleanup. The host supplies the NVIDIA driver.\n",
        )
        manifest = _profile_manifest(profile_root, candidate, producer, recipe_hash)
        _write_json(profile_root / "profile-manifest.json", manifest)

        final_asset, checksum_out, provenance_out = _write_release_assets(
            stage, profile_root, promotion
        )
        shutil.rmtree(stage / "candidate")
        os.replace(stage, output_dir)
        return (
            output_dir / PROFILE_ROOT_NAME,
            output_dir / final_asset.name,
            output_dir / checksum_out.name,
            output_dir / provenance_out.name,
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse candidate promotion inputs without side effects."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-archive", type=Path, required=True)
    parser.add_argument("--acceptance-receipt", type=Path, required=True)
    parser.add_argument("--expected-model-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--promotion-source-sha", required=True)
    parser.add_argument("--promotion-source-ref", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run promotion and report errors without exposing evidence contents."""
    args = parse_args(argv)
    try:
        paths = promote_candidate(
            candidate_archive=args.candidate_archive,
            acceptance_path=args.acceptance_receipt,
            expected_model_sha256=args.expected_model_sha256,
            output_dir=args.output_dir,
            promotion_source_sha=args.promotion_source_sha,
            promotion_source_ref=args.promotion_source_ref,
        )
    except (PromotionError, OSError, tarfile.TarError) as error:
        print(f"promotion failed: {error}", file=sys.stderr)
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
