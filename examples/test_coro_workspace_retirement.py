from __future__ import annotations

import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class CoroWorkspaceRetirementTest(unittest.TestCase):
    def test_supported_workspace_variants(self) -> None:
        for project in ("profiling", "conformance"):
            with self.subTest(project=project):
                profile = runpy.run_path(str(ROOT / project / "profile_workspace.py"))
                self.assertEqual(profile["VARIANTS"]["cppcoro"], "cppcoroexample")
                self.assertNotIn("cppboost", profile["VARIANTS"])
                self.assertIn("cppcoroservicelib", profile["FRAMEWORK_REPOSITORIES"])
                self.assertNotIn("cppboostservicelib", profile["FRAMEWORK_REPOSITORIES"])

    def test_coro_uses_own_archive_without_legacy_adaptation(self) -> None:
        for project, graph_profile in (
            ("profiling", "function-call"),
            ("profiling", "current"),
            ("conformance", "current"),
        ):
            with self.subTest(project=project, profile=graph_profile):
                self.prepare_workspace(project, graph_profile, missing_archive=False)

    def test_missing_coro_archive_does_not_fall_back_to_boost(self) -> None:
        for project in ("profiling", "conformance"):
            with self.subTest(project=project):
                self.prepare_workspace(project, "current", missing_archive=True)

    def prepare_workspace(
        self, project: str, graph_profile: str, *, missing_archive: bool
    ) -> None:
        profile = runpy.run_path(str(ROOT / project / "profile_workspace.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            source_root.mkdir()
            repositories = (
                "cppcoroexample", "cppcoroservicelib", "cppboostexample",
                "cppboostservicelib", "cppboostnativeexample",
            )
            for repository in repositories:
                source = source_root / repository
                source.mkdir()
                (source / "user_owned.txt").write_text(repository)

            generated_profiles: list[str] = []
            merge_commands: list[list[str]] = []
            snapshots: list[str] = []

            def generate_archives(source, archives, selected_profile):
                self.assertEqual(source, source_root)
                generated_profiles.append(selected_profile)
                (archives / "cppboost.zip").write_bytes(b"must not be used")
                if not missing_archive:
                    (archives / "cppcoro.zip").write_bytes(b"coro fixture")
                return "generated"

            def merge(command, *, cwd, **kwargs):
                self.assertEqual(command[:3], ["bash", "scripts/merge.generated.sh", "--remove-stale"])
                self.assertEqual(Path(command[-1]).name, "cppcoro.zip")
                self.assertEqual(Path(cwd).name, "cppcoroexample")
                merge_commands.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="merged")

            def snapshot(path, *args):
                snapshots.append(path.name)

            with mock.patch.dict(profile["prepare"].__globals__, {
                "VARIANTS": {"cppcoro": "cppcoroexample"},
                "FRAMEWORK_REPOSITORIES": {"cppcoroservicelib"},
                "ARTIFACTS": root / "artifacts",
                "generate_archives": generate_archives,
                "verify_graph": lambda *args: {"verified": 1},
                "verify_current_graph": lambda *args: {"verified": 1},
                "initialize_git_snapshot": snapshot,
                "release_tags_at_head": lambda *args: ["v-test"],
                "run": merge,
            }):
                workspace = root / "workspace"
                if missing_archive:
                    with self.assertRaisesRegex(RuntimeError, r"missing generated profile archive: .*cppcoro\.zip"):
                        profile["prepare"](source_root, workspace, graph_profile)
                    self.assertEqual(merge_commands, [])
                    return
                profile["prepare"](source_root, workspace, graph_profile)

            self.assertEqual(generated_profiles, [graph_profile])
            self.assertEqual(len(merge_commands), 1)
            self.assertCountEqual(snapshots, ["cppcoroexample", "cppcoroservicelib"])
            for repository in ("cppcoroexample", "cppcoroservicelib"):
                self.assertFalse((workspace / repository).is_symlink())
                self.assertEqual((workspace / repository / "user_owned.txt").read_text(), repository)
            self.assertTrue((workspace / "cppboostnativeexample").is_symlink())
            for retired in ("cppboostexample", "cppboostservicelib"):
                self.assertFalse((workspace / retired).exists())
                self.assertTrue((source_root / retired / "user_owned.txt").is_file())
            summary = json.loads((root / "artifacts" / f"profile-{graph_profile}" / "summary.json").read_text())
            self.assertEqual(summary["generated_graphs"], {"cppcoro": {"verified": 1}})


if __name__ == "__main__":
    unittest.main()
