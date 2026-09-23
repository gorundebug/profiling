#!/usr/bin/env python3
"""Apply perf's symbol root to addr2line subprocesses (including stripped DSOs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_arguments(arguments: list[str], symbol_root: str) -> list[str]:
    root = Path(symbol_root).resolve(strict=True)

    def resolve(filename: str) -> str:
        candidate = Path(filename)
        if not candidate.is_absolute():
            raise ValueError(f"perf supplied a non-absolute ELF path: {filename}")
        if not candidate.is_relative_to(root):
            candidate = root / filename.lstrip("/")
        candidate = candidate.resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError(f"ELF path escapes the captured symbol root: {filename}")
        if not candidate.is_file():
            raise ValueError(f"ELF path is not a file: {candidate}")
        return str(candidate)

    result = arguments.copy()
    for index, argument in enumerate(arguments):
        if argument in ("-e", "--exe"):
            if index + 1 == len(arguments):
                raise ValueError("addr2line executable argument is missing")
            result[index + 1] = resolve(arguments[index + 1])
        elif argument.startswith("--exe="):
            result[index] = "--exe=" + resolve(argument[len("--exe="):])
    return result


def main() -> int:
    try:
        arguments = resolve_arguments(sys.argv[1:], os.environ["PERF_SYMBOL_ROOT"])
        os.execv("/usr/bin/addr2line", ["addr2line", *arguments])
    except (OSError, ValueError, KeyError) as error:
        # perf 6.1 discards addr2line stderr. Preserve path failures separately.
        diagnostic = os.environ.get("PERF_SYMBOL_DIAGNOSTICS")
        message = f"addr2line symbol-root error: {error}\n"
        if diagnostic:
            with open(diagnostic, "a") as handle:
                handle.write(message)
        sys.stderr.write(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
