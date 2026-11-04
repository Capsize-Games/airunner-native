#!/usr/bin/env python3
"""Assemble a hash-locked, relocatable CUDA SM 120a sidecar candidate."""

from __future__ import annotations

import argparse
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
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from native_recipe_identity import verify_identity


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "runtime_sidecars" / "cuda-sm120-package-lock.json"
PROFILE_PATH = ROOT / "runtime_sidecars" / "cuda-sm120-candidate.env"
CPU_OUTPUT_ROOT = ROOT / "build" / "runtime-sidecars"
OUTPUT_MARKER = ".airunner-native-cuda-candidate"
OUTPUT_MARKER_VALUE = "airunner-native-cuda-candidate-v1:cuda-sm120"
ARCHIVE_ROOT_NAME = "airunner-native-linux-x86_64-cuda-sm120"
MAX_LOCK_BYTES = 256 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 1_500_000_000
CHUNK_SIZE = 1024 * 1024


class PackageError(RuntimeError):
    """An input or candidate artifact failed its locked validation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PackageError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackageError(f"{label} must be a regular file, not a symlink or special file: {path}")
    return path


def read_json(path: Path, label: str) -> dict[str, Any]:
    require_regular_file(path, label)
    if path.stat().st_size > MAX_LOCK_BYTES:
        raise PackageError(f"{label} exceeds the size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{label} must be a JSON object")
    return value


def parse_kv_file(path: Path, label: str) -> dict[str, str]:
    require_regular_file(path, label)
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            # The build receipt appends the full --version stdout/stderr block
            # after its locked key/value fields.
            if label == "candidate provenance":
                continue
            raise PackageError(f"invalid {label} entry at line {number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z0-9_]+", key) or key in values:
            raise PackageError(f"invalid or duplicate {label} key at line {number}")
        values[key] = value
    return values


def parse_status(path: Path) -> dict[str, str]:
    # The build receipt intentionally appends a terminal status to its initial running record.
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        if "=" not in raw:
            raise PackageError(f"invalid candidate status at line {number}")
        key, value = raw.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise PackageError(f"invalid candidate status key at line {number}")
        values[key] = value
    return values


def ensure_disjoint(path: Path, other: Path, label: str) -> None:
    p = path.resolve(strict=False)
    o = other.resolve(strict=False)
    if p == o or p in o.parents or o in p.parents:
        raise PackageError(f"{label} overlaps protected path: {other}")


def validate_platform() -> None:
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise PackageError("CUDA SM 120a packaging is supported only on Linux x86_64")


def reviewed_source_patch(lock: dict[str, Any]) -> dict[str, str]:
    """Require the one hash-pinned source correction supported by this recipe."""
    patches = lock["source"].get("patches_applied")
    if not isinstance(patches, list) or len(patches) != 1 or not isinstance(patches[0], dict):
        raise PackageError("package lock must identify exactly one reviewed source patch")
    patch = patches[0]
    if (patch.get("path") != "runtime_sidecars/patches/llama.cpp-cuda-all-mtp-offload.patch"
            or not re.fullmatch(r"[0-9a-f]{64}", str(patch.get("sha256", "")))):
        raise PackageError("package lock source patch identity is malformed")
    return patch


def copy_reviewed_source_patch(source_patch: dict[str, str], recipe_root: Path) -> None:
    """Retain the exact reviewed patch bytes beside the producer recipe."""
    source = require_regular_file(ROOT / source_patch["path"], "reviewed source patch")
    if sha256_file(source) != source_patch["sha256"]:
        raise PackageError("reviewed source patch changed during candidate packaging")
    shutil.copyfile(source, recipe_root / "llama.cpp-cuda-all-mtp-offload.patch")


def validate_build_inputs(build_output: Path, receipt_path: Path, lock: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    marker = require_regular_file(build_output / ".airunner-native-candidate", "candidate marker")
    if marker.read_text(encoding="utf-8").strip() != "airunner-native-candidate-v1:cuda-sm120":
        raise PackageError("build output marker does not identify the CUDA SM 120 candidate")

    status_path = require_regular_file(build_output / "candidate-status.env", "candidate status")
    provenance_path = require_regular_file(build_output / "candidate-provenance.env", "candidate provenance")
    server_path = require_regular_file(build_output / "llama-server", "llama-server")
    license_path = require_regular_file(build_output / "LICENSE-llama.cpp.txt", "llama.cpp license")
    hash_path = require_regular_file(build_output / "llama-server.sha256", "server SHA-256 receipt")
    status = parse_status(status_path)
    if status.get("status") != "success":
        raise PackageError(f"candidate build did not finish successfully: {status.get('status', 'missing')}")
    server_hash = sha256_file(server_path)
    expected_server_hash = status.get("server_sha256", "")
    if server_hash != expected_server_hash:
        raise PackageError("candidate server bytes do not match the terminal build-status hash")
    hash_receipt = hash_path.read_text(encoding="utf-8").split()
    if not hash_receipt or hash_receipt[0] != server_hash:
        raise PackageError("candidate server bytes do not match llama-server.sha256")
    if sha256_file(license_path) != lock["expected_source_license_sha256"]:
        raise PackageError("llama.cpp license differs from the locked source license")

    provenance = parse_kv_file(provenance_path, "candidate provenance")
    build = lock["build"]
    source = lock["source"]
    patch = reviewed_source_patch(lock)
    expected = {
        "source_commit": source["commit"],
        "source_tree": source["tree"],
        "source_version": source["version"],
        "source_build_number": str(source["build_number"]),
        "source_patch_sha256": patch["sha256"],
        "source_patch_path": patch["path"],
        "cuda_image": build["image"],
        "cuda_architectures": build["cuda_architectures"],
        "cmake_version": build["cmake_version"],
        "cmake_archive_sha256": build["cmake_archive_sha256"],
        "cmake_binary_sha256": build["cmake_binary_sha256"],
    }
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise PackageError(f"candidate build provenance has wrong {key}")
    version_line = next((line for line in provenance_path.read_text(encoding="utf-8").splitlines() if line.startswith("version:")), "")
    if source["commit"] not in version_line or str(source["build_number"]) not in version_line:
        raise PackageError("candidate version string does not identify the locked source build")

    receipt = read_json(receipt_path, "build container receipt")
    receipt_expected = {
        "image": build["image"],
        "runtime": "runc",
        "network": "none",
        "gpu_device_requests": None,
        "cpu_limit": 2,
        "memory_limit": "8g",
        "server_sha256": server_hash,
    }
    for key, value in receipt_expected.items():
        if receipt.get(key) != value:
            raise PackageError(f"build container receipt has wrong {key}")
    required_constraints = {
        "--platform=linux/amd64",
        "--pull=never",
        "--runtime=runc",
        "--network=none",
        "--cpus=2",
        "--memory=8g",
        "--pids-limit=1024",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
    }
    command_constraints = receipt.get("command_constraints")
    if not isinstance(command_constraints, list) or not required_constraints.issubset(set(command_constraints)):
        raise PackageError("build container receipt omits required isolation or resource constraints")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("container_id", ""))):
        raise PackageError("build container receipt has no full container ID")
    if "exit 0" not in str(receipt.get("result", "")):
        raise PackageError("build container receipt does not record successful completion")
    return status, provenance, receipt


def validate_zip_names(archive: zipfile.ZipFile, label: str) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        mode_type = stat.S_IFMT(info.external_attr >> 16)
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.is_absolute()
        ):
            raise PackageError(f"unsafe member path in {label}: {name!r}")
        if mode_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise PackageError(f"non-regular member in {label}: {name!r}")
        if name in members:
            raise PackageError(f"duplicate member in {label}: {name!r}")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise PackageError(f"oversized member in {label}: {name!r}")
        members[name] = info
    return members


def extract_locked_member(
    wheel_path: Path,
    wheel_lock: dict[str, Any],
    member: str,
    expected_sha256: str,
    expected_size: int,
    destination: Path,
) -> None:
    require_regular_file(wheel_path, f"{wheel_lock['filename']} wheel")
    if wheel_path.stat().st_size != wheel_lock["size_bytes"]:
        raise PackageError(f"{wheel_lock['filename']} archive size does not match lock")
    if sha256_file(wheel_path) != wheel_lock["sha256"]:
        raise PackageError(f"{wheel_lock['filename']} archive SHA-256 does not match lock")
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = validate_zip_names(archive, wheel_lock["filename"])
            info = members.get(member)
            if info is None or info.is_dir():
                raise PackageError(f"locked member is missing: {member}")
            if info.file_size != expected_size:
                raise PackageError(f"locked member size mismatch: {member}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            copied = 0
            try:
                with archive.open(info, "r") as source, destination.open("xb") as target:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > expected_size:
                            raise PackageError(f"member exceeds locked size: {member}")
                        digest.update(chunk)
                        target.write(chunk)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            if copied != expected_size or digest.hexdigest() != expected_sha256:
                destination.unlink(missing_ok=True)
                raise PackageError(f"locked member SHA-256 or size mismatch: {member}")
    except zipfile.BadZipFile as exc:
        raise PackageError(f"invalid wheel archive: {wheel_lock['filename']}") from exc


def extract_locked_wheel(
    wheel_path: Path,
    wheel_lock: dict[str, Any],
    license_member_sha256: str,
    license_member_size_bytes: int,
    package_root: Path,
    license_destination: str,
) -> None:
    """Verify a wheel once, then stream only its locked regular-file members."""
    require_regular_file(wheel_path, f"{wheel_lock['filename']} wheel")
    if wheel_path.stat().st_size != wheel_lock["size_bytes"]:
        raise PackageError(f"{wheel_lock['filename']} archive size does not match lock")
    if sha256_file(wheel_path) != wheel_lock["sha256"]:
        raise PackageError(f"{wheel_lock['filename']} archive SHA-256 does not match lock")
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = validate_zip_names(archive, wheel_lock["filename"])
            expected = [
                (member["member"], member["destination"], member["sha256"], member["size_bytes"])
                for member in wheel_lock["members"]
            ]
            expected.append((wheel_lock["license_member"], license_destination,
                             license_member_sha256, license_member_size_bytes))
            for member_name, destination_name, digest, size in expected:
                info = members.get(member_name)
                if info is None or info.is_dir():
                    raise PackageError(f"locked member is missing: {member_name}")
                if info.file_size != size:
                    raise PackageError(f"locked member size mismatch: {member_name}")
                destination = package_root / destination_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                hasher = hashlib.sha256()
                copied = 0
                try:
                    with archive.open(info, "r") as source, destination.open("xb") as target:
                        while True:
                            chunk = source.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > size:
                                raise PackageError(f"member exceeds locked size: {member_name}")
                            hasher.update(chunk)
                            target.write(chunk)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                if copied != size or hasher.hexdigest() != digest:
                    destination.unlink(missing_ok=True)
                    raise PackageError(f"locked member SHA-256 or size mismatch: {member_name}")
                os.chmod(destination, 0o644)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"invalid wheel archive: {wheel_lock['filename']}") from exc


def readelf_dynamic(path: Path) -> tuple[set[str], str | None]:
    executable = shutil.which("readelf")
    if not executable:
        raise PackageError("readelf is required to validate the candidate ELF dependency tags")
    try:
        result = subprocess.run(
            [executable, "-d", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise PackageError(f"readelf timed out for {path.name}") from exc
    if result.returncode:
        raise PackageError(f"readelf failed for {path.name}: {result.stderr[-2000:]}")
    needed = set(re.findall(r"\(NEEDED\).*?\[([^]]+)\]", result.stdout))
    runpath_match = re.search(r"\((?:RUNPATH|RPATH)\).*?\[([^]]*)\]", result.stdout)
    runpath = runpath_match.group(1) if runpath_match else None
    if "/" in (runpath or "") and not (runpath or "").startswith("$ORIGIN"):
        raise PackageError(f"absolute ELF RPATH/RUNPATH is forbidden: {path.name}: {runpath}")
    return needed, runpath


def validate_elf_tree(package_root: Path, lock: dict[str, Any]) -> None:
    expected_needed = lock["build"]["expected_needed"]
    for rel, expected_runpath in lock["build"]["expected_runpaths"].items():
        path = package_root / rel
        needed, runpath = readelf_dynamic(path)
        if runpath != expected_runpath:
            raise PackageError(f"unexpected RUNPATH for {rel}: {runpath!r}")
        if needed != set(expected_needed[rel]):
            raise PackageError(f"unexpected DT_NEEDED closure for {rel}: {sorted(needed)}")
    wrapper = package_root / "bin" / "llama-server-cuda"
    if "LD_LIBRARY_PATH=$lib_dir" not in wrapper.read_text(encoding="utf-8") or "cd /" not in wrapper.read_text(encoding="utf-8"):
        raise PackageError("launcher does not constrain the NVRTC empty-RUNPATH component")


def copy_regular(source: Path, destination: Path, expected_sha256: str, label: str) -> None:
    require_regular_file(source, label)
    if sha256_file(source) != expected_sha256:
        raise PackageError(f"{label} SHA-256 does not match lock")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o755 if destination.name in {"llama-server", "llama-server-cuda"} else 0o644)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(path, 0o644)


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def sha_inventory(root: Path, *, exclude: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PackageError(f"candidate archive may not contain symlinks: {path}")
        if path.is_file() and path.relative_to(root).as_posix() not in exclude:
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return rows


def write_file_hashes(root: Path) -> None:
    rows = sha_inventory(root, exclude={"SHA256SUMS", "provenance/file-manifest.json"})
    write_json(root / "provenance" / "file-manifest.json", {
        "schema": "candidate-file-manifest-v1",
        "algorithm": "SHA-256",
        "files": rows,
    })
    sums = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.relative_to(root).as_posix() != "SHA256SUMS":
            sums.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    write_text(root / "SHA256SUMS", "\n".join(sums) + "\n")


def create_deterministic_tar_gz(root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent)
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("wb") as raw, gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=1) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                entries = [root] + sorted(root.rglob("*"))
                for path in entries:
                    rel = Path(ARCHIVE_ROOT_NAME) if path == root else Path(ARCHIVE_ROOT_NAME) / path.relative_to(root)
                    info = tarfile.TarInfo(rel.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    if path.is_dir():
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.size = 0
                        tar.addfile(info)
                    elif path.is_file():
                        info.type = tarfile.REGTYPE
                        info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                        info.size = path.stat().st_size
                        with path.open("rb") as stream:
                            tar.addfile(info, stream)
                    else:
                        raise PackageError(f"unsupported candidate entry: {path}")
        if archive_path.exists() or archive_path.is_symlink():
            raise PackageError(f"refusing to overwrite archive: {archive_path}")
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, archive_path)
    finally:
        temp_path.unlink(missing_ok=True)


def package_candidate(
    *,
    build_output: Path,
    build_receipt_path: Path,
    runtime_wheel: Path,
    cublas_wheel: Path,
    nvrtc_wheel: Path,
    gcc_notice: Path,
    platform_packages: Path,
    output_dir: Path,
    archive_path: Path,
) -> str:
    validate_platform()
    lock = read_json(LOCK_PATH, "CUDA package lock")
    if lock.get("schema_version") != 1:
        raise PackageError("unsupported CUDA package lock schema")

    protected = [
        ROOT / ".git",
        ROOT / ".github",
        ROOT / "scripts",
        ROOT / "runtime_sidecars",
        ROOT / "tests",
        ROOT / "review",
        CPU_OUTPUT_ROOT,
        LOCK_PATH,
        PROFILE_PATH,
        build_output,
        build_receipt_path,
        runtime_wheel,
        cublas_wheel,
        nvrtc_wheel,
        gcc_notice,
        platform_packages,
    ]
    out_resolved = output_dir.resolve(strict=False)
    archive_resolved = archive_path.resolve(strict=False)
    for protected_path in protected:
        ensure_disjoint(out_resolved, protected_path, "package output")
        ensure_disjoint(archive_resolved, protected_path, "archive output")
    ensure_disjoint(out_resolved, archive_resolved, "package directory and archive")
    if output_dir.exists() or output_dir.is_symlink():
        raise PackageError(f"refusing to overwrite package directory: {output_dir}")
    if archive_path.exists() or archive_path.is_symlink():
        raise PackageError(f"refusing to overwrite archive: {archive_path}")
    if Path(str(archive_path) + ".sha256").exists() or Path(str(archive_path) + ".sha256").is_symlink():
        raise PackageError(f"refusing to overwrite archive checksum: {archive_path}.sha256")

    validate_build_inputs(build_output, build_receipt_path, lock)
    build_lock = lock["build"]
    profile = parse_kv_file(PROFILE_PATH, "CUDA candidate profile")
    for key, value in {
        "LLAMA_CPP_COMMIT": lock["source"]["commit"],
        "LLAMA_CPP_TREE": lock["source"]["tree"],
        "LLAMA_CPP_VERSION": lock["source"]["version"],
        "LLAMA_CPP_PATCH_PATH": lock["source"]["patches_applied"][0]["path"],
        "LLAMA_CPP_PATCH_SHA256": lock["source"]["patches_applied"][0]["sha256"],
        "CUDA_IMAGE": build_lock["image"],
        "CUDA_ARCHITECTURES": build_lock["cuda_architectures"],
        "CMAKE_VERSION": build_lock["cmake_version"],
        "CMAKE_ARCHIVE_SHA256": build_lock["cmake_archive_sha256"],
        "CMAKE_BINARY_SHA256": build_lock["cmake_binary_sha256"],
    }.items():
        if profile.get(key) != value:
            raise PackageError(f"candidate build profile has wrong {key}")
    if sha256_file(gcc_notice) != lock["gcc_runtime"]["copyright_notice_sha256"]:
        raise PackageError("GCC runtime copyright notice differs from lock")
    if sha256_file(platform_packages) != lock["gcc_runtime"]["packages_file_sha256"]:
        raise PackageError("platform runtime package list differs from lock")
    status, provenance, receipt = validate_build_inputs(build_output, build_receipt_path, lock)
    producer = verify_identity(build_output / "producer-recipe.json", "cuda-sm120a")

    parent = output_dir.parent.resolve(strict=False)
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent))
    try:
        root = temp_dir
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        (root / "licenses").mkdir()
        (root / "provenance" / "recipe").mkdir(parents=True)
        copy_regular(build_output / "llama-server", root / "bin/llama-server", status["server_sha256"], "candidate server")
        copy_regular(build_output / "LICENSE-llama.cpp.txt", root / "licenses/LICENSE-llama.cpp.txt", lock["expected_source_license_sha256"], "llama.cpp license")
        launcher = """#!/bin/sh
set -eu
server_path=$(readlink -f -- "$0")
server_dir=${server_path%/*}
lib_dir=$(CDPATH= cd -- "$server_dir/../lib" && pwd -P)
cd /
LD_LIBRARY_PATH=$lib_dir
export LD_LIBRARY_PATH
exec "$server_dir/llama-server" "$@"
"""
        write_text(root / "bin/llama-server-cuda", launcher)
        os.chmod(root / "bin/llama-server-cuda", 0o755)

        wheel_paths = {
            "nvidia-cuda-runtime-cu12": runtime_wheel,
            "nvidia-cublas-cu12": cublas_wheel,
            "nvidia-cuda-nvrtc-cu12": nvrtc_wheel,
        }
        wheel_lock = lock["reviewed_wheel_lock"]["wheels"]
        license_destinations = {
            "nvidia-cuda-runtime-cu12": "licenses/LICENSE-nvidia-cuda-runtime-cu12.txt",
            "nvidia-cublas-cu12": "licenses/LICENSE-nvidia-cublas-cu12.txt",
            "nvidia-cuda-nvrtc-cu12": "licenses/LICENSE-nvidia-cuda-nvrtc-cu12.txt",
        }
        for package, wheel_path in wheel_paths.items():
            extract_locked_wheel(
                wheel_path,
                wheel_lock[package],
                lock["reviewed_wheel_lock"]["license_member_sha256"],
                lock["reviewed_wheel_lock"]["license_member_size_bytes"],
                root,
                license_destinations[package],
            )

        copy_regular(gcc_notice, root / "licenses/GCC-libgomp-copyright.txt", lock["gcc_runtime"]["copyright_notice_sha256"], "GCC runtime notice")
        copy_regular(platform_packages, root / "provenance/platform-runtime-packages.txt", lock["gcc_runtime"]["packages_file_sha256"], "platform runtime package list")
        validate_elf_tree(root, lock)

        for name in ("candidate-provenance.env", "candidate-status.env", "build.log", "llama-server.sha256"):
            source_file = require_regular_file(build_output / name, f"build evidence {name}")
            destination = root / "provenance" / name
            shutil.copyfile(source_file, destination)
        for name in ("cuda-compile-flags.make", "elf-cuda-inspection.txt", "cuda-build-config.txt"):
            source_file = require_regular_file(build_output / name, f"CUDA build evidence {name}")
            destination = root / "provenance" / name
            shutil.copyfile(source_file, destination)
        compile_flags = (root / "provenance/cuda-compile-flags.make").read_text(encoding="utf-8")
        if "--generate-code=arch=compute_120a,code=[sm_120a]" not in compile_flags:
            raise PackageError("NVCC compile flags do not contain the locked SM 120a cubin target")
        cubin_report = (root / "provenance/elf-cuda-inspection.txt").read_text(encoding="utf-8")
        cubins = re.findall(r"sm_([0-9]+[a-z]?)\.cubin", cubin_report)
        if cubins.count("120a") != build_lock["expected_cubins"]["sm_120a"] or set(cubins) != {"120a"}:
            raise PackageError("cuobjdump report does not contain the locked SM 120a-only cubin set")
        config_text = (root / "provenance/cuda-build-config.txt").read_text(encoding="utf-8")
        if lock["source"]["commit"] not in config_text or "CUDA enabled=ON" not in config_text:
            raise PackageError("CUDA build configuration receipt does not match the locked source and target")
        shutil.copyfile(build_receipt_path, root / "provenance/build-container-receipt.json")
        write_json(root / "provenance/producer-recipe.json", producer)
        shutil.copyfile(ROOT / "scripts/native_recipe_identity.py", root / "provenance/recipe/native_recipe_identity.py")
        shutil.copyfile(PROFILE_PATH, root / "provenance/recipe/cuda-sm120-candidate.env")
        shutil.copyfile(LOCK_PATH, root / "provenance/recipe/cuda-sm120-package-lock.json")
        copy_reviewed_source_patch(reviewed_source_patch(lock), root / "provenance/recipe")
        shutil.copyfile(Path(__file__), root / "provenance/recipe/package_cuda_sm120_candidate.py")
        build_script = ROOT / "scripts" / "build_cuda_sm120_candidate.sh"
        shutil.copyfile(build_script, root / "provenance/recipe/build_cuda_sm120_candidate.sh")

        v5_input = {
            "schema": "reviewed-nvidia-wheel-inputs-v1",
            "v5_selected_artifacts_lock_sha256": lock["reviewed_wheel_lock"]["v5_selected_artifacts_lock_sha256"],
            "static_wheel_review_lock_sha256": lock["reviewed_wheel_lock"]["static_wheel_review_lock_sha256"],
            "license_expression_in_review_metadata": lock["reviewed_wheel_lock"]["license_expression"],
            "license_member_sha256": lock["reviewed_wheel_lock"]["license_member_sha256"],
            "items": [
                {"package": package, **wheel_lock[package]}
                for package in sorted(wheel_paths)
            ],
            "review_scope_note": "The exact selected wheel archives and members were statically reviewed. The metadata label is not a legal conclusion."
        }
        write_json(root / "provenance/nvidia-v5-wheel-inputs.json", v5_input)
        write_json(root / "provenance/image-library-comparison.json", lock["image_library_comparison"])
        write_json(root / "provenance/platform-runtime-dependencies.json", {
            "schema": "linux-cuda-sidecar-platform-dependencies-v1",
            "platform_scope": lock["target"],
            "verified_against_image": build_lock["image"],
            "required_external_libraries": [
                {"soname": "libcuda.so.1", "provider": "NVIDIA host driver", "included": False},
                {"soname": "libgomp.so.1", "provider": f"{lock['gcc_runtime']['package']} {lock['gcc_runtime']['version']}", "included": False,
                 "image_file_sha256": lock["gcc_runtime"]["library_sha256"], "notice_file": "../licenses/GCC-libgomp-copyright.txt"},
                {"soname": "libc.so.6, libm.so.6, libdl.so.2, libpthread.so.0, librt.so.1, libstdc++.so.6, libgcc_s.so.1, ld-linux-x86-64.so.2",
                 "provider": "Ubuntu 24.04 platform runtime packages listed in platform-runtime-packages.txt", "included": False,
                 "required_abi_floor_observed": "GLIBC_2.38 and GLIBCXX_3.4.32"}
            ],
            "not_a_portability_claim": "No claim is made for older distributions, non-x86_64 systems, other libc implementations, or driver compatibility."
        })
        write_json(root / "provenance/manifest.json", {
            "schema": "airunner-native-cuda-sidecar-candidate-v1",
            "artifact": "llama-server CUDA 12.9 / SM 120a candidate",
            "status": "candidate only; not published, activated, or GPU-runtime tested",
            "source": lock["source"],
            "producer": producer,
            "build": {
                **build_lock,
                "server_sha256": status["server_sha256"],
                "runpath": "$ORIGIN/../lib",
                "launch_entrypoint": "bin/llama-server-cuda",
                "container_id": receipt["container_id"],
                "compiler": next((line.removeprefix("built with ") for line in (build_output / "candidate-provenance.env").read_text(encoding="utf-8").splitlines() if line.startswith("built with ")), "recorded in candidate provenance"),
            },
            "patches_applied": lock["source"]["patches_applied"],
            "reviewed_dynamic_loads": lock["reviewed_dynamic_loads"],
            "validations": ["version/help checks completed in isolated build container", "readelf dependency tags and RUNPATHs checked", "static SM120 cubin receipt checked", "wheel archives and selected member hashes checked"],
            "not_established": ["GPU runtime compatibility", "model inference or performance", "service integration or candidate activation"]
        })
        write_text(root / "THIRD-PARTY-NOTICES.md", """# Third-party notices

The candidate includes llama.cpp-derived server code and exact library members from the selected NVIDIA wheels listed in `provenance/nvidia-v5-wheel-inputs.json`.

- `licenses/LICENSE-llama.cpp.txt` contains the MIT license from the locked llama.cpp source revision.
- The NVIDIA `LICENSE.txt` members from the exact selected cuBLAS, CUDA runtime, and NVRTC wheels are preserved as separate files under `licenses/`.
- `licenses/GCC-libgomp-copyright.txt` is the notice from the Ubuntu 24.04 `libgomp1` package. `libgomp.so.1` is a platform dependency and is not included.

The server requires `libcuda.so.1` from the host NVIDIA driver. No driver, compatibility library, or CUDA stub is included. See `provenance/platform-runtime-dependencies.json`.
""")
        write_text(root / "README.md", """# Linux CUDA SM 120a sidecar candidate

This archive contains an unpublished CUDA `llama-server` candidate built from the pinned llama.cpp revision. Use `bin/llama-server-cuda` as the entry point. The launcher sets `LD_LIBRARY_PATH` to this archive's `lib/` directory and changes the child working directory to `/` to contain the upstream NVRTC RUNPATH's trailing empty component. Direct invocation of `bin/llama-server` bypasses that containment.

CUDA runtime, cuBLAS, cuBLASLt, and NVRTC library members are extracted from the exact V5-selected wheels locked by SHA-256. The build image's cuBLAS, cuBLASLt, and NVRTC bytes differ from those wheel members; the artifact therefore uses the statically reviewed wheel members. The NVRTC and cuBLAS version combination has not been tested on a GPU.

The artifact targets Linux x86_64 with an Ubuntu 24.04 ABI baseline. The host provides `libcuda.so.1`, `libgomp.so.1`, and the GNU C/C++ runtime. No model or GPU runtime execution was performed. Build, source, license, and artifact hashes are recorded under `provenance/`.
""")
        write_file_hashes(root)
        create_deterministic_tar_gz(root, archive_path)
        archive_hash = sha256_file(archive_path)
        write_text(Path(str(archive_path) + ".sha256"), f"{archive_hash}  {archive_path.name}\n")
        os.chmod(root, 0o755)
        os.replace(root, output_dir)
        return archive_hash
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if archive_path.exists():
            archive_path.unlink()
        sidecar = Path(str(archive_path) + ".sha256")
        sidecar.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-output", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--runtime-wheel", type=Path, required=True)
    parser.add_argument("--cublas-wheel", type=Path, required=True)
    parser.add_argument("--nvrtc-wheel", type=Path, required=True)
    parser.add_argument("--gcc-notice", type=Path, required=True)
    parser.add_argument("--platform-packages", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        archive_hash = package_candidate(
            build_output=args.build_output.resolve(strict=True),
            build_receipt_path=args.build_receipt.resolve(strict=True),
            runtime_wheel=args.runtime_wheel.resolve(strict=True),
            cublas_wheel=args.cublas_wheel.resolve(strict=True),
            nvrtc_wheel=args.nvrtc_wheel.resolve(strict=True),
            gcc_notice=args.gcc_notice.resolve(strict=True),
            platform_packages=args.platform_packages.resolve(strict=True),
            output_dir=args.output_dir.resolve(strict=False),
            archive_path=args.archive.resolve(strict=False),
        )
    except (PackageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"candidate archive: {args.archive}")
    print(f"sha256: {archive_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
