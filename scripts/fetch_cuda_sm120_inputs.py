#!/usr/bin/env python3
"""Fetch the hash-locked public CUDA candidate build inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "runtime_sidecars" / "cuda-sm120-package-lock.json"
TIMEOUT_SECONDS = 45
CHUNK_SIZE = 1024 * 1024


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def fetch_one(url: str, destination: Path, expected_size: int, expected_sha256: str) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing to replace existing download: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        request = Request(url, headers={"User-Agent": "airunner-native-candidate-input-fetch/1"})
        with os.fdopen(fd, "wb") as target, urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            while block := response.read(CHUNK_SIZE):
                size += len(block)
                if size > expected_size:
                    raise ValueError(f"download exceeded locked size: {url}")
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise ValueError(f"download size or SHA-256 mismatch: {url}")
        os.replace(temp_path, destination)
    except (OSError, URLError, TimeoutError, ValueError):
        temp_path.unlink(missing_ok=True)
        raise


def fetch_all(output: Path) -> list[Path]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"download directory must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    build = lock["build"]
    downloads: list[tuple[str, str, int, str]] = [
        (
            build["cmake_archive_url"],
            "cmake-3.31.6-linux-x86_64.tar.gz",
            build["cmake_archive_size_bytes"],
            build["cmake_archive_sha256"],
        )
    ]
    for wheel in lock["reviewed_wheel_lock"]["wheels"].values():
        downloads.append((wheel["source_url"], wheel["filename"], wheel["size_bytes"], wheel["sha256"]))
    completed: list[Path] = []
    try:
        for url, filename, size, sha256 in downloads:
            if not url.startswith(("https://github.com/Kitware/", "https://files.pythonhosted.org/")):
                raise ValueError(f"unexpected build-input host: {url}")
            target = output / filename
            fetch_one(url, target, size, sha256)
            completed.append(target)
        return completed
    except Exception:
        for path in completed:
            path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        for path in fetch_all(args.output_dir.resolve(strict=False)):
            print(f"{digest_file(path)}  {path}")
    except Exception as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
