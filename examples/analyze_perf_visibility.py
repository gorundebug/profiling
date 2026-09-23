#!/usr/bin/env python3
"""Report unresolved physical frames separately from leaf-only flamegraph labels."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Iterable


FRAME = re.compile(r"^\s+([0-9a-fA-F]+)\s+(.+)\s+\((.*)\)\s*$")


def summarize_visibility(lines: Iterable[str]) -> dict:
    counts = Counter(samples=0, samples_with_unresolved_frames=0, unknown_leaf_samples=0,
                     physical_frames=0, unresolved_frames=0, inline_frames=0,
                     samples_ending_in_unknown=0)
    libraries = Counter()
    addresses = Counter()
    frames = []

    def finish() -> None:
        if not frames:
            return
        counts["samples"] += 1
        unresolved = [item for item in frames if item[2]]
        counts["samples_with_unresolved_frames"] += bool(unresolved)
        counts["unknown_leaf_samples"] += frames[0][2]
        counts["samples_ending_in_unknown"] += frames[-1][2]
        counts["physical_frames"] += len(frames)
        counts["unresolved_frames"] += len(unresolved)
        for address, library, _ in unresolved:
            libraries[library] += 1
            addresses[(library, address)] += 1
        frames.clear()

    for line in lines:
        match = FRAME.match(line)
        if match:
            address, symbol, library = match.groups()
            if library == "inlined":
                counts["inline_frames"] += 1
            else:
                frames.append((address, library, symbol.startswith(("[", "0x"))))
        elif not line.strip() or not line[0].isspace():
            finish()
    finish()
    return {**counts, "unresolved_by_library": dict(libraries.most_common()),
            "top_unresolved_addresses": [dict(library=dso, address=ip, occurrences=n)
                                         for (dso, ip), n in addresses.most_common(30)],
            "note": "Counts are not CPU percentages. A named leaf does not prove a complete stack. "
                    "An unknown outermost frame does not alone prove unwind truncation."}


if __name__ == "__main__":
    with open(sys.argv[1]) as handle:
        result = summarize_visibility(handle)
    print(json.dumps(result, indent=2))
    if result["unresolved_frames"]:
        print(f"WARNING: {result['samples_with_unresolved_frames']}/{result['samples']} sampled stacks "
              "contain unresolved frames; inspect .visibility.json", file=sys.stderr)
