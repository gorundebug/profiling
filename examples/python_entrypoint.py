#!/usr/bin/env python3

"""Run a generated Python service without per-request benchmark I/O."""

from __future__ import annotations

import logging
import runpy
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python_entrypoint.py <service.module> [service arguments]"
        )
    module, module_arguments = sys.argv[1], sys.argv[2:]
    logging.getLogger("aiohttp.access").disabled = True
    logging.getLogger("aiohttp.server").setLevel(logging.ERROR)
    sys.argv = [module, *module_arguments]
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
