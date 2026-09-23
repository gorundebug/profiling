#!/usr/bin/env python3
"""Reject empty/unsymbolized perf output instead of publishing thread-only graphs."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable


FRAME = re.compile(r"^\s+[0-9a-fA-F]+\s+(.+)\s+\((/.*|\[.*\])\)\s*$")


def summarize(lines: Iterable[str]) -> dict[str, int]:
    counts = {"frames": 0, "named_function_frames": 0, "named_user_function_frames": 0, "unresolved_frames": 0}
    for line in lines:
        frame = FRAME.match(line)
        if frame is None:
            continue
        counts["frames"] += 1
        symbol = frame.group(1)
        if symbol.startswith("[") or symbol.startswith("0x"):
            counts["unresolved_frames"] += 1
        else:
            counts["named_function_frames"] += 1
            if frame.group(2).startswith("/") and not frame.group(2).endswith(".ko"):
                counts["named_user_function_frames"] += 1
    return counts


def main() -> int:
    with open(sys.argv[1]) as handle:
        counts = summarize(handle)
    print(json.dumps(counts, indent=2))
    if counts["named_user_function_frames"] == 0:
        print("perf decoding produced no named user-space function frames; refusing an unusable flamegraph", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
