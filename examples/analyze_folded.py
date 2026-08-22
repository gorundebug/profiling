#!/usr/bin/env python3
"""Summarize a collapsed-stack file (stackcollapse-perf.pl / py-spy `-f raw`
format: `frame1;frame2;...;frameN <weight>` per line) into ranked self-time
and total-time tables with real percentages of the sampled total.

Usage: analyze_folded.py <folded.txt> [top-n] > <summary.txt>
"""

from __future__ import annotations

import sys
from collections import defaultdict


def analyze(path: str, top: int) -> str:
    self_time: dict[str, int] = defaultdict(int)
    total_time: dict[str, int] = defaultdict(int)
    total = 0

    with open(path) as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            stack_part, _, weight_str = line.rpartition(" ")
            if not stack_part:
                continue
            try:
                weight = int(weight_str)
            except ValueError:
                continue
            total += weight
            frames = stack_part.split(";")
            self_time[frames[-1]] += weight
            # A frame that recurses within one stack should only count once
            # toward that stack's contribution to its inclusive time.
            for frame in set(frames):
                total_time[frame] += weight

    lines = [f"source: {path}", f"total weight: {total}", ""]
    if total == 0:
        lines.append("(no samples)")
        return "\n".join(lines) + "\n"

    lines.append(f"-- Top {top} by SELF time (leaf frame) --")
    for func, weight in sorted(self_time.items(), key=lambda item: -item[1])[:top]:
        lines.append(f"{100 * weight / total:6.2f}%  {weight:>14}  {func}")

    lines.append("")
    lines.append(f"-- Top {top} by TOTAL time (inclusive, any frame in stack) --")
    for func, weight in sorted(total_time.items(), key=lambda item: -item[1])[:top]:
        lines.append(f"{100 * weight / total:6.2f}%  {weight:>14}  {func}")

    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: analyze_folded.py <folded.txt> [top-n]", file=sys.stderr)
        return 2
    path = sys.argv[1]
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    sys.stdout.write(analyze(path, top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
