import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("artifact_directory_runner", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class ArtifactDirectoryTests(unittest.TestCase):
    def test_cli_selects_directory_before_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "isolated"
            observed = []
            with patch.object(runner, "ARTIFACTS", runner.ARTIFACTS), \
                 patch.object(runner, "acquire_tooling_lock"), \
                 patch.object(runner, "clean", side_effect=lambda: observed.append(runner.ARTIFACTS)), \
                 patch.object(sys, "argv", ["run.py", "--artifacts-dir", str(target), "--clean"]):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(observed, [target.resolve()])

    def test_default_still_uses_existing_directory(self):
        original = runner.ARTIFACTS
        with patch.object(runner, "ARTIFACTS", original), \
             patch.object(runner, "acquire_tooling_lock"), \
             patch.object(runner, "clean") as cleanup, \
             patch.object(sys, "argv", ["run.py", "--clean"]):
            self.assertEqual(runner.main(), 0)
            self.assertEqual(runner.ARTIFACTS, original.resolve())
            cleanup.assert_called_once_with()

    def test_environment_and_logs_use_selected_root(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            args = argparse.Namespace(duration="20s", loadgen_cores=6, cores=2, vus=256,
                                      graph_profile="function-call", scenario="normal", rate=100000)
            language = next(item for item in runner.LANGUAGES if item.name == "go")
            with patch.object(runner, "ARTIFACTS", target):
                env = runner.environment(args, language)
                self.assertEqual(env["PROFILING_ARTIFACTS_DIR"], str(target))
                self.assertEqual(env["PROFILING_ALLOCATOR_LIBRARY"], str(target / "liballocation_profile.so"))
                self.assertEqual(runner.language_log_path(args, "go"), target / "logs/function-call/go.log")

    def test_backend_runs_can_have_distinct_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            observed = []
            for backend in ("epoll", "uring"):
                target = Path(directory) / backend
                with patch.object(runner, "ARTIFACTS", runner.ARTIFACTS), \
                     patch.object(runner, "acquire_tooling_lock"), \
                     patch.object(runner, "clean", side_effect=lambda: observed.append(runner.ARTIFACTS)), \
                     patch.object(sys, "argv", ["run.py", "--artifacts-dir", str(target), "--clean"]):
                    self.assertEqual(runner.main(), 0)
            self.assertNotEqual(*observed)


if __name__ == "__main__":
    unittest.main()
