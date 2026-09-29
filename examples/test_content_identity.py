from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from content_identity import content_source_identity


class ContentIdentityTests(unittest.TestCase):
    def test_records_local_files_without_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new.cpp").write_text("local change\n")
            with patch("subprocess.run", side_effect=AssertionError("no subprocess")):
                result = content_source_identity(root)
            self.assertEqual(result["mode"], "content")
            self.assertEqual(result["entry_count"], 1)
            self.assertIn("new.cpp", result["entries"])
            self.assertNotIn("revision", result)

    def test_deterministic_across_root_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b"):
                target = root / name
                target.mkdir()
                (target / "file").write_bytes(b"same")
            a = content_source_identity(root / "a")
            b = content_source_identity(root / "b")
            self.assertEqual(a["tree_sha256"], b["tree_sha256"])

    def test_changes_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / "file"
            item.write_text("a")
            before = content_source_identity(root)
            item.write_text("b")
            after = content_source_identity(root)
            self.assertNotEqual(before["tree_sha256"], after["tree_sha256"])
            item.chmod(0o755)
            self.assertNotEqual(after["tree_sha256"], content_source_identity(root)["tree_sha256"])

    def test_excludes_only_exact_cache_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("build", ".git", "build_substream_analytics_result"):
                target = root / name
                target.mkdir()
                (target / "source.cpp").write_text("x")
            result = content_source_identity(root)
            self.assertEqual(list(result["entries"]), ["build_substream_analytics_result/source.cpp"])
            self.assertEqual(result["excluded_directories"], [".git", "build"])

    def test_symlinks_are_explicit_not_recursively_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "loop").symlink_to(root, target_is_directory=True)
            (root / "missing").symlink_to("absent")
            result = content_source_identity(root)
            self.assertEqual(result["entry_count"], 2)
            self.assertEqual(result["entries"]["missing"], {"kind": "symlink", "target": "absent"})

    def test_missing_root_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                content_source_identity(Path(directory) / "missing")


if __name__ == "__main__":
    unittest.main()
