from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.fetch_cuda_sm120_inputs import fetch_all, fetch_one


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FetchCudaInputsTests(unittest.TestCase):
    def test_fetch_writes_only_locked_bytes(self):
        body = b"reviewed input"
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "input.whl"
            with patch("scripts.fetch_cuda_sm120_inputs.urlopen", return_value=Response(body)):
                fetch_one("https://files.pythonhosted.org/locked.whl", destination, len(body), hashlib.sha256(body).hexdigest())
            self.assertEqual(destination.read_bytes(), body)

    def test_hash_or_size_mismatch_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "input.tar.gz"
            with patch("scripts.fetch_cuda_sm120_inputs.urlopen", return_value=Response(b"wrong")):
                with self.assertRaisesRegex(ValueError, "size or SHA-256 mismatch"):
                    fetch_one("https://github.com/Kitware/CMake/releases/file", destination, 5, "0" * 64)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_existing_destination_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "input.whl"
            destination.write_bytes(b"preserve")
            with patch("scripts.fetch_cuda_sm120_inputs.urlopen") as open_url:
                with self.assertRaisesRegex(ValueError, "refusing to replace"):
                    fetch_one("https://files.pythonhosted.org/locked.whl", destination, 7, "0" * 64)
            open_url.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"preserve")

    def test_fetch_all_uses_locked_kitware_and_python_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []

            def fake_fetch(url, destination, size, digest):
                self.assertTrue(url.startswith(("https://github.com/Kitware/", "https://files.pythonhosted.org/")))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"x")
                paths.append(destination)

            with patch("scripts.fetch_cuda_sm120_inputs.fetch_one", side_effect=fake_fetch):
                result = fetch_all(root / "inputs")
            self.assertEqual(result, paths)
            self.assertEqual(len(result), 4)  # CMake plus the three locked NVIDIA wheels.
            self.assertEqual(len({path.name for path in result}), 4)

    def test_populated_download_directory_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "inputs"
            output.mkdir()
            (output / "unrelated").write_text("keep", encoding="utf-8")
            with patch("scripts.fetch_cuda_sm120_inputs.fetch_one") as fetch:
                with self.assertRaisesRegex(ValueError, "must be absent or empty"):
                    fetch_all(output)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
