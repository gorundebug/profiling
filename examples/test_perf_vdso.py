import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_perf_vdso as vdso


class VdsoCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "symbols"
        self.proc = Path(self.directory.name) / "proc"
        (self.proc / "123").mkdir(parents=True)
        (self.proc / "123" / "maps").write_text("1000-1010 r-xp 00000000 00:00 0 [vdso]\n")
        (self.proc / "123" / "mem").write_bytes(b"")
        self.buildids = Path(self.directory.name) / "buildids.txt"

    def capture(self, data=b"\x7fELF" + b"\x00" * 12, build_id="1234abcd"):
        with patch.object(vdso.os, "pread", return_value=data) as read, \
                patch.object(vdso, "elf_info", return_value={"build_id": build_id}):
            result = vdso.capture(123, self.root, self.proc)
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1:], (16, 0x1000))
        return result

    def test_only_exact_executable_vdso_mapping(self):
        self.assertEqual(vdso.vdso_range("1000-2000 r-xp 0 00:00 0 [vdso]"), (4096, 8192))
        for maps in ("1000-2000 r-xp 0 00:00 0 /tmp/vdso.so", "",
                     "1000-2000 r--p 0 00:00 0 [vdso]",
                     "2000-1000 r-xp 0 00:00 0 [vdso]",
                     "1000-2000000 r-xp 0 00:00 0 [vdso]"):
            with self.subTest(maps=maps), self.assertRaises(ValueError):
                vdso.vdso_range(maps)

    def test_capture_preserves_exact_target_bytes(self):
        report = self.capture()
        self.assertEqual(report["status"], "captured")
        self.assertEqual((self.root / "[vdso]").read_bytes(), b"\x7fELF" + b"\x00" * 12)
        self.assertEqual(json.loads((self.root / ".vdso.json").read_text()), report)

    def test_short_read_removes_stale_image(self):
        self.capture()
        self.assertEqual(self.capture(data=b"\x7fELF")["status"], "unavailable")
        self.assertFalse((self.root / "[vdso]").exists())

    def test_missing_build_id_is_not_accepted(self):
        self.assertEqual(self.capture(build_id=None)["status"], "unavailable")
        self.assertFalse((self.root / "[vdso]").exists())

    def test_non_elf_is_not_accepted(self):
        self.assertEqual(self.capture(data=b"x" * 16)["status"], "unavailable")

    def test_permission_failure_is_reported_without_stale_image(self):
        self.capture()
        with patch.object(vdso.os, "pread", side_effect=PermissionError("denied")):
            report = vdso.capture(123, self.root, self.proc)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("denied", report["error"])
        self.assertFalse((self.root / "[vdso]").exists())

    def test_recorded_build_id_must_match(self):
        for recorded, status in (("1234abcd", "verified"), ("feedface", "unavailable")):
            with self.subTest(recorded=recorded):
                self.capture()
                self.buildids.write_text(f"{recorded} [vdso]\n")
                with patch.object(vdso, "elf_info", return_value={"build_id": "1234abcd"}):
                    report = vdso.verify(self.root, self.buildids)
                self.assertEqual(report["status"], status)
                self.assertEqual((self.root / "[vdso]").exists(), status == "verified")

    def test_unsampled_is_not_claimed_verified(self):
        self.capture()
        self.buildids.write_text("abcdef12 /usr/bin/service\n")
        with patch.object(vdso, "elf_info", return_value={"build_id": "1234abcd"}):
            self.assertEqual(vdso.verify(self.root, self.buildids)["status"], "not-sampled")

    def test_changed_capture_is_rejected(self):
        self.capture()
        self.buildids.write_text("1234abcd [vdso]\n")
        with patch.object(vdso, "elf_info", return_value={"build_id": "feedface"}):
            self.assertEqual(vdso.verify(self.root, self.buildids)["status"], "unavailable")
        self.assertFalse((self.root / "[vdso]").exists())


if __name__ == "__main__":
    unittest.main()
