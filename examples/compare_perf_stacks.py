#!/usr/bin/env python3
"""Compare two decodings without treating fewer unknowns as proof of correctness."""

import argparse
from collections import Counter
from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import re
import sys


FRAME = re.compile(r"^\s+([0-9a-fA-F]+)\s+(.+?)\s*$")
HEADER = re.compile(r"\s\d+(?:/\d+)?\s+(?:\[\d+\]\s+)?\d+\.\d+:.*:\s*$")


@dataclass
class Sample:
    header: str
    frames: list


def parse_frame(line):
    match = FRAME.match(line)
    if match is None:
        return None
    address, text = match.groups()
    if not text.endswith(")"):
        return None
    # Only the final parenthesized group is the DSO/inline marker. C++
    # symbols and DSO paths can themselves contain nested parentheses.
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] == ")":
            depth += 1
        elif text[index] == "(":
            depth -= 1
            if depth == 0:
                if index == 0 or not text[index - 1].isspace():
                    return None
                name = text[:index].rstrip()
                if not name:
                    return None
                return address, name, text[index + 1:-1]
    return None


def samples(lines):
    current = None
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        frame = parse_frame(line)
        if frame:
            if current is None:
                raise ValueError(f"frame before sample header at line {number}")
            address, name, library = frame
            if library != "inlined":
                current.frames.append((int(address, 16), name, library))
        elif HEADER.search(line):
            if current is not None:
                yield current
            current = Sample(" ".join(line.split()), [])
        else:
            raise ValueError(f"unsupported perf script line {number}: {line[:160].rstrip()}")
    if current is not None:
        yield current


def known(frame):
    return frame[1] not in ("[unknown]", "??")


def compare(before, after):
    counts = Counter()
    examples = []
    for index, pair in enumerate(itertools.zip_longest(before, after), 1):
        old, new = pair
        counts["baseline_samples"] += old is not None
        counts["candidate_samples"] += new is not None
        if old is None or new is None or old.header != new.header:
            counts["sample_identity_or_weight_mismatches"] += 1
            if len(examples) < 10:
                examples.append({"sample": index, "baseline": old.header if old else None,
                                 "candidate": new.header if new else None})
            continue
        counts["matched_samples"] += 1
        for label, sample in (("baseline", old), ("candidate", new)):
            counts[f"{label}_empty_stacks"] += not sample.frames
            counts[f"{label}_physical_frames"] += len(sample.frames)
            counts[f"{label}_unknown_frames"] += sum(not known(f) for f in sample.frames)
        counts["shorter_stacks"] += len(new.frames) < len(old.frames)
        counts["longer_stacks"] += len(new.frames) > len(old.frames)
        counts["changed_physical_stacks"] += old.frames != new.frames
        old_known = Counter(f[0] for f in old.frames if known(f))
        new_known = Counter(f[0] for f in new.frames if known(f))
        counts["stacks_losing_previously_named_addresses"] += bool(old_known - new_known)
        if old.frames and new.frames:
            counts["changed_leaf_addresses"] += old.frames[0][0] != new.frames[0][0]
    return {
        "same_sample_sequence_and_weights": not counts["sample_identity_or_weight_mismatches"],
        "counts": dict(sorted(counts.items())),
        "identity_mismatch_examples": examples,
        "note": "Fewer unknowns, longer stacks, or unchanged sample weights do not prove correct unwinding. "
                "Changed chains require validation against unwind metadata or a known-call-chain fixture. "
                "Shorter stacks and lost named addresses are review signals, not proof that the baseline was correct.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        with args.baseline.open() as old, args.candidate.open() as new:
            result = compare(samples(old), samples(new))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["same_sample_sequence_and_weights"] else 1


if __name__ == "__main__":
    sys.exit(main())
