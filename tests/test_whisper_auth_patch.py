"""Integrity and build wiring checks for the reviewed whisper auth patch."""

from __future__ import annotations

import unittest
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "runtime_sidecars/patches/whisper.cpp-v1.9.5-api-auth.patch"
PINS = ROOT / "runtime_sidecars/runtime_pins.env"
BUILD = ROOT / "scripts/build_runtime_sidecars.sh"


def _pins() -> dict[str, str]:
    result = {}
    for line in PINS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        result[name] = value
    return result


class WhisperAuthPatchTests(unittest.TestCase):
    def test_patch_hash_and_upstream_commit_are_bound_in_pins(self) -> None:
        pins = _pins()
        assert pins["WHISPER_CPP_COMMIT"] == "d1be6fde11ac6e0407606b4e42fe72d34add8037"
        assert pins["WHISPER_CPP_VERSION"] == "v1.9.5"
        assert pins["WHISPER_CPP_AUTH_PATCH_SHA256"] == hashlib.sha256(
            PATCH.read_bytes()
        ).hexdigest()


    def test_build_applies_exact_patch_before_configuration_and_records_identity(self) -> None:
        build = BUILD.read_text(encoding="utf-8")
        patch_check = build.index('git -C "$source_path" apply --check "$patch_path"')
        patch_apply = build.index('git -C "$source_path" apply "$patch_path"')
        configure = build.index('configure_build \\\n        "$source_path"', patch_apply)
        assert patch_check < patch_apply < configure
        assert "AIRUNNER_WHISPER_CPP_AUTH_PATCH_SHA256" in build
        assert "current_patch_hash" in build


    def test_patch_guards_every_route_before_routing_with_exact_health_exception(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        assert "set_pre_routing_handler" in patch
        assert 'req.method == "GET" && req.path == health_path' in patch
        assert 'res.status = 401' in patch
        assert 'req.get_header_value("Authorization")' in patch
        assert "--api-key-file" in patch
        assert "non-loopback binds" in patch
        assert "is_loopback_bind_host(sparams.hostname)" in patch


    def test_patch_preserves_pre_routing_401_through_whisper_error_handler(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        assert "svr->set_error_handler(whisper_server_auth::handle_server_error)" in patch
        assert "res.status != 401 && res.status != 500" in patch


    def test_patch_rejects_missing_cli_value_and_reads_key_with_a_bound(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        assert "i + 1 >= argc || argv[i + 1][0] == '\\0'" in patch
        assert "std::array<char, 4098> buffer" in patch
        assert "count == buffer.size()" in patch
        assert "key.size() > 4096" in patch
