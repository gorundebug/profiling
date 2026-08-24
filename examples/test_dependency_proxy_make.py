#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DependencyProxyMakeTest(unittest.TestCase):
    def test_direct_make_enables_the_dependency_proxy(self) -> None:
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("SERVICEGEN_DEPENDENCY_PROXY_DIR", makefile)
        self.assertIn(
            "export SERVICEGEN_GITHUB_RAW_URL := "
            "$(SERVICEGEN_DEPENDENCY_PROXY_BASE)/github-raw",
            makefile,
        )
        self.assertIn("scripts/dependency-proxy-bin:$(PATH)", makefile)
        launcher = ROOT / "scripts/dependency-proxy-bin/docker"
        self.assertTrue(os.access(launcher, os.X_OK))


if __name__ == "__main__":
    unittest.main()
