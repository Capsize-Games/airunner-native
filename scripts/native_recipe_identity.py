"""Bind native build receipts to a clean, committed producer recipe."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "cpu": (
        "scripts/native_recipe_identity.py",
        "scripts/build_runtime_sidecars.sh",
        "runtime_sidecars/runtime_pins.env",
        "runtime_sidecars/patches/whisper.cpp-v1.9.5-api-auth.patch",
    ),
    "cuda-sm120a": (
        "scripts/native_recipe_identity.py",
        "scripts/build_cuda_sm120_candidate.sh",
        "scripts/package_cuda_sm120_candidate.py",
        "scripts/fetch_cuda_sm120_inputs.py",
        "runtime_sidecars/cuda-sm120-candidate.env",
        "runtime_sidecars/cuda-sm120-package-lock.json",
        "runtime_sidecars/patches/llama.cpp-cuda-all-mtp-offload.patch",
        "runtime_sidecars/licenses/GCC-libgomp-copyright.txt",
        "runtime_sidecars/ubuntu-24.04-platform-runtime-packages.txt",
    ),
}


class IdentityError(RuntimeError):
    """The producer source or recorded identity cannot be verified."""


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.PIPE, timeout=15
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise IdentityError("Cannot verify the committed producer source") from exc


def capture_identity(profile: str, root: Path = ROOT) -> dict[str, Any]:
    """Require clean tracked sources and committed bytes for every recipe input."""
    if profile not in PROFILES:
        raise IdentityError("Unknown native recipe profile")
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise IdentityError("Producer tracked source has uncommitted changes")
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode().strip()
    recipes = {}
    for relative in PROFILES[profile]:
        path = root / relative
        if not stat.S_ISREG(path.lstat().st_mode):
            raise IdentityError(f"Recipe input must be a regular file: {relative}")
        actual = path.read_bytes()
        if actual != _git(root, "show", f"HEAD:{relative}"):
            raise IdentityError(f"Recipe input differs from committed bytes: {relative}")
        recipes[relative] = hashlib.sha256(actual).hexdigest()
    return {
        "schema": "airunner-native-producer-recipe-v1",
        "repository": "Capsize-Games/airunner-native",
        "profile": profile,
        "commit": commit,
        "tree": tree,
        "recipes_sha256": recipes,
    }


def verify_identity(path: Path, profile: str, root: Path = ROOT) -> dict[str, Any]:
    """Reject packaging or completion under a different producer recipe."""
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 128 * 1024:
        raise IdentityError("Producer receipt is not a bounded regular file")
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise IdentityError("Producer receipt is not valid JSON") from exc
    if recorded != capture_identity(profile, root):
        raise IdentityError("Build producer identity differs from the packaging recipe")
    return recorded


def main() -> None:
    """Capture a new receipt or verify a previously captured receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "verify"))
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "verify":
        verify_identity(args.output, args.profile)
    else:
        identity = capture_identity(args.profile)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream, indent=2, sort_keys=True)
            stream.write("\n")


if __name__ == "__main__":
    main()
