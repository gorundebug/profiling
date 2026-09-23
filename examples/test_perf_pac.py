import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_perf_pac as pac


class PerfPacTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "perf.data"
        self.data.write_bytes(b"recorded samples")
        self.identity = dict(pid=123, start_time="42", boot_id="boot", elf_machine=183)
        self.report = dict(self.identity, version=1, status="captured", instruction_mask="0x7f000000000000")

    def test_mask_is_kernel_value_not_a_guessed_address_width(self):
        for mask in ("0x0", "0x1234", "0xffffffffffffffff"):
            self.assertEqual(pac.mask_value(mask), mask)
        for mask in ("", None, "-1", "0x10000000000000000", "0x123z", "$(command)"):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                pac.mask_value(mask)

    def test_capture_checks_process_identity(self):
        result = subprocess.CompletedProcess([], 0, "0x7f000000000000\n", "")
        with patch.object(pac, "process_identity", return_value=self.identity), \
                patch.object(pac.subprocess, "run", return_value=result):
            self.assertEqual(pac.capture(123), self.report)
        changed = dict(self.identity, start_time="43")
        with patch.object(pac, "process_identity", side_effect=[self.identity, changed]), \
                patch.object(pac.subprocess, "run", return_value=result):
            self.assertEqual(pac.capture(123)["status"], "unavailable")

    def test_non_arm_target_is_not_traced(self):
        with patch.object(pac, "process_identity", return_value=dict(self.identity, elf_machine=62)), \
                patch.object(pac.subprocess, "run") as run:
            self.assertEqual(pac.capture(123)["status"], "not-applicable")
            run.assert_not_called()

    def test_denied_capture_is_visible(self):
        with patch.object(pac, "process_identity", return_value=self.identity), \
                patch.object(pac.subprocess, "run", side_effect=PermissionError("denied")):
            report = pac.capture(123)
            self.assertEqual(report["status"], "unavailable")
            self.assertIn("denied", report["error"])

    def test_binding_checks_state_again_and_recording_hash(self):
        with patch.object(pac, "capture", return_value=self.report):
            report = pac.bind(self.report, 123, self.data)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(pac.decode_environment(report, self.data, {})[pac.MASK_ENV], self.report["instruction_mask"])
        self.data.write_bytes(b"another recording")
        with self.assertRaises(ValueError):
            pac.decode_environment(report, self.data, {})

    def test_changed_mask_is_not_applied(self):
        with patch.object(pac, "capture", return_value=dict(self.report, instruction_mask="0x0")):
            report = pac.bind(self.report, 123, self.data)
        self.assertEqual(report["status"], "unavailable")
        self.assertNotIn(pac.MASK_ENV, pac.decode_environment(report, self.data, {pac.MASK_ENV: "stale"}))

    def test_wrong_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            pac.bind(self.report, 456, self.data)

    def test_unbound_and_wrong_architecture_are_rejected(self):
        for report in (self.report, dict(self.report, status="verified", elf_machine=62)):
            with self.subTest(report=report), self.assertRaises(ValueError):
                pac.decode_environment(dict(report, perf_sha256=pac.digest(self.data)), self.data, {})

    def test_unavailable_clears_inherited_mask(self):
        report = dict(self.report, status="unavailable", perf_sha256=pac.digest(self.data))
        self.assertEqual(pac.decode_environment(report, self.data, {pac.MASK_ENV: "stale", "PATH": "/bin"}), {"PATH": "/bin"})


if __name__ == "__main__":
    unittest.main()
