"""Producer receipts reject changed, missing, or uncommitted build recipes."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_recipe_identity import IdentityError, PROFILES, capture_identity, verify_identity


class RecipeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet")
        for relative in PROFILES["cpu"]:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture recipe\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), "-c", "user.name=Fixture",
             "-c", "user.email=fixture@example.invalid", *args],
            stderr=subprocess.PIPE, timeout=10,
        )

    def receipt(self):
        path = self.root / "build-receipt.json"
        identity = capture_identity("cpu", self.root)
        path.write_text(json.dumps(identity), encoding="utf-8")
        return path, identity

    def test_roundtrip_records_the_committed_revision_and_recipe_hashes(self):
        path, identity = self.receipt()
        self.assertEqual(identity["commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(set(identity["recipes_sha256"]), set(PROFILES["cpu"]))
        self.assertEqual(verify_identity(path, "cpu", self.root), identity)

    def test_uncommitted_recipe_is_rejected(self):
        path = self.root / PROFILES["cpu"][1]
        path.write_text("changed recipe")
        with self.assertRaisesRegex(IdentityError, "uncommitted"):
            capture_identity("cpu", self.root)

    def test_untracked_replacement_is_not_a_committed_recipe(self):
        relative = PROFILES["cpu"][1]
        self.git("rm", "--", relative)
        self.git("commit", "--quiet", "-m", "remove recipe")
        (self.root / relative).write_text("untracked replacement")
        with self.assertRaisesRegex(IdentityError, "committed producer"):
            capture_identity("cpu", self.root)

    def test_rebuilt_recipe_revision_cannot_relabel_an_old_build(self):
        path, _ = self.receipt()
        (self.root / PROFILES["cpu"][1]).write_text("new committed recipe")
        self.git("add", PROFILES["cpu"][1])
        self.git("commit", "--quiet", "-m", "update recipe")
        with self.assertRaisesRegex(IdentityError, "differs"):
            verify_identity(path, "cpu", self.root)

    def test_symlink_receipt_is_rejected(self):
        path, _ = self.receipt()
        link = self.root / "receipt-link.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(IdentityError, "regular file"):
            verify_identity(link, "cpu", self.root)


if __name__ == "__main__":
    unittest.main()
