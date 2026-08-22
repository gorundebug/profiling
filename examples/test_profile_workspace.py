from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path


PROFILING = Path(__file__).resolve().parents[1]


class ProfileWorkspaceTest(unittest.TestCase):
    def test_current_profile_requires_every_call_semantics(self) -> None:
        profile = runpy.run_path(str(PROFILING / "profile_workspace.py"))
        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory)
            graph = example / "graph"
            graph.mkdir()
            (graph / "example.generated.yaml").write_text(
                "callSemantics: TaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
            )
            self.assertEqual(
                profile["verify_current_graph"](example),
                {
                    "task_pool_links": 1,
                    "priority_task_pool_links": 1,
                    "parallel_call_links": 3,
                },
            )


if __name__ == "__main__":
    unittest.main()
