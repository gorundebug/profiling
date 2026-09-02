#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DependencyProxyMakeTest(unittest.TestCase):
    def test_direct_make_enables_the_dependency_proxy(self) -> None:
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("DEPENDENCY_PROXY_DIR", makefile)
        self.assertIn(
            "export DEPENDENCY_GITHUB_RAW_URL := "
            "$(DEPENDENCY_PROXY_BASE)/github-raw",
            makefile,
        )
        self.assertIn("scripts/dependency-proxy-bin:$(PATH)", makefile)
        launcher = ROOT / "scripts/dependency-proxy-bin/docker"
        self.assertTrue(os.access(launcher, os.X_OK))
        launcher_text = launcher.read_text()
        self.assertIn("cppexample/scripts/docker-dependency-proxy.generated.sh", launcher_text)
        self.assertNotIn("docker-dependency-proxy.sh", launcher_text)


if __name__ == "__main__":
    unittest.main()
