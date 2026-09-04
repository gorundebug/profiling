from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import dependency_command


class DependencyCommandTest(unittest.TestCase):
    def test_retries_transient_network_failure(self) -> None:
        failed = mock.Mock(stdout=iter(["context deadline exceeded\n"]))
        failed.wait.return_value = 1
        passed = mock.Mock(stdout=iter(["done\n"]))
        passed.wait.return_value = 0
        with mock.patch.object(
            dependency_command.subprocess, "Popen", side_effect=[failed, passed]
        ) as popen, mock.patch.object(dependency_command.time, "sleep"):
            result = dependency_command.run(
                ["docker", "build", "."], cwd=Path("."), env={}, echo=False
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(popen.call_count, 2)

    def test_does_not_retry_compile_failure(self) -> None:
        failed = mock.Mock(stdout=iter(["undefined reference to symbol\n"]))
        failed.wait.return_value = 1
        with mock.patch.object(
            dependency_command.subprocess, "Popen", return_value=failed
        ) as popen, self.assertRaises(subprocess.CalledProcessError):
            dependency_command.run(
                ["make", "docker-build"], cwd=Path("."), env={}, echo=False
            )
        self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
