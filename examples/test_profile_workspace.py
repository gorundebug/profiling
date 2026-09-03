from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROFILING = Path(__file__).resolve().parents[1]


class ProfileWorkspaceTest(unittest.TestCase):
    def test_archive_generation_passes_canonical_profile_contract(self) -> None:
        profile = runpy.run_path(str(PROFILING / "profile_workspace.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            servicegen = root / "servicegen"
            servicegen.mkdir()
            archive_dir = root / "archives"
            with mock.patch.object(profile["subprocess"], "run") as run:
                run.return_value.stdout = "generated"
                self.assertEqual(
                    profile["generate_archives"](root, archive_dir, "current"),
                    "generated",
                )
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["EXAMPLE_PROFILE"], "current")
            self.assertNotIn("SERVICEGEN_EXAMPLE_PROFILE", environment)

    def test_dependency_proxy_contract_has_no_generator_namespace(self) -> None:
        forbidden = "SERVICE" + "GEN_"
        offenders = []
        for path in PROFILING.rglob("*"):
            if not path.is_file() or any(
                part in {".git", ".dependencies", ".artifacts", "build", "__pycache__"}
                for part in path.parts
            ):
                continue
            if path.suffix not in {".py", ".sh", ".mk", ".yml", ".yaml", ".md"} and path.name not in {"Makefile", "Dockerfile"}:
                continue
            text = path.read_text(errors="ignore")
            if any(
                f"{forbidden}{token}" in text
                for token in ("DEPENDENCY", "NEXUS", "GIT_MIRROR", "GITHUB_RAW", "GITLAB_RAW", "MAVEN", "APT_", "HELM_")
            ):
                offenders.append(str(path.relative_to(PROFILING)))
        self.assertEqual(offenders, [])

    def test_current_profile_requires_every_call_semantics(self) -> None:
        profile = runpy.run_path(str(PROFILING / "profile_workspace.py"))
        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory)
            graph = example / "graph"
            graph.mkdir()
            (graph / "example.generated.yaml").write_text(
                "callSemantics: TaskPool\n"
                "callSemantics: TaskPool\n"
                "callSemantics: TaskPool\n"
                "callSemantics: TaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
            )
            self.assertEqual(
                profile["verify_current_graph"](example),
                {
                    "task_pool_links": 4,
                    "priority_task_pool_links": 4,
                    "parallel_call_links": 3,
                },
            )


if __name__ == "__main__":
    unittest.main()
