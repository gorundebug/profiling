#!/usr/bin/env python3

import argparse
import bisect
import collections
import json
from pathlib import Path
import re
import struct
import subprocess


HEADER = struct.Struct("<8sIIQQ")
MAP = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+)\s+(\S+)\s+([0-9a-f]+)\s+"
    r"\S+\s+\S+\s*(.*)$"
)
SKIP = (
    "backtrace",
    "maybe_sample_allocation",
    "record_allocation",
    "allocation_profile.c",
)
ALLOCATOR_ENTRYPOINTS = {
    "malloc",
    "calloc",
    "realloc",
    "aligned_alloc",
    "memalign",
    "posix_memalign",
}


def is_profiler_frame(frame: str) -> bool:
    function = frame.split(" (", 1)[0]
    return (
        any(skip in frame for skip in SKIP)
        or function in ALLOCATOR_ENTRYPOINTS
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--folded", type=Path, required=True)
    parser.add_argument("--bytes-folded", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--maps-output", type=Path, required=True)
    return parser.parse_args()


def read_records(path: Path) -> tuple[dict[str, int], list[dict[str, object]]]:
    data = path.read_bytes()
    if len(data) < HEADER.size:
        raise RuntimeError(f"truncated allocation stack header: {path}")
    magic, version, frame_count, pid, sample_every = HEADER.unpack_from(data)
    if magic != b"SLASTK\0\0" or version != 1 or not 1 <= frame_count <= 256:
        raise RuntimeError(f"invalid allocation stack header: {path}")
    record_struct = struct.Struct(f"<QQQII{frame_count}Q")
    payload = data[HEADER.size :]
    if len(payload) % record_struct.size:
        raise RuntimeError(f"truncated allocation stack record: {path}")
    records = []
    for offset in range(0, len(payload), record_struct.size):
        generation, sequence, usable_size, kind, depth, *frames = (
            record_struct.unpack_from(payload, offset)
        )
        if depth > frame_count:
            raise RuntimeError(f"invalid allocation stack depth {depth}: {path}")
        records.append(
            {
                "generation": generation,
                "sequence": sequence,
                "usable_size": usable_size,
                "kind": kind,
                "frames": frames[:depth],
            }
        )
    if not records:
        raise RuntimeError(f"allocation stack profile contains no samples: {path}")
    generation = max(int(record["generation"]) for record in records)
    records = [
        record for record in records if int(record["generation"]) == generation
    ]
    return {
        "pid": pid,
        "sample_every": sample_every,
        "frame_count": frame_count,
        "generation": generation,
    }, records


def read_maps(pid: int, output: Path) -> tuple[list[int], list[tuple[int, int, int, str]]]:
    text = Path(f"/proc/{pid}/maps").read_text()
    output.write_text(text)
    mappings = []
    for line in text.splitlines():
        match = MAP.match(line)
        if not match:
            continue
        start, end, permissions, offset, path = match.groups()
        path = path.removesuffix(" (deleted)")
        if "x" not in permissions or not path.startswith("/"):
            continue
        mappings.append((int(start, 16), int(end, 16), int(offset, 16), path))
    mappings.sort()
    return [mapping[0] for mapping in mappings], mappings


def resolve_address(
    address: int,
    starts: list[int],
    mappings: list[tuple[int, int, int, str]],
) -> tuple[str, int] | None:
    index = bisect.bisect_right(starts, address) - 1
    if index < 0:
        return None
    start, end, offset, path = mappings[index]
    if address >= end:
        return None
    return path, address - start + offset


def symbolize(
    pid: int, locations: set[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    grouped: dict[str, list[int]] = collections.defaultdict(list)
    for path, address in locations:
        grouped[path].append(address)
    symbols: dict[tuple[str, int], str] = {}
    for path, addresses in grouped.items():
        object_path = Path(f"/proc/{pid}/root") / path.lstrip("/")
        unique = sorted(set(addresses))
        for begin in range(0, len(unique), 256):
            chunk = unique[begin : begin + 256]
            command = [
                "addr2line",
                "-C",
                "-f",
                "-e",
                str(object_path),
                *[f"0x{address:x}" for address in chunk],
            ]
            try:
                result = subprocess.run(
                    command, check=True, capture_output=True, text=True
                ).stdout.splitlines()
            except (OSError, subprocess.CalledProcessError):
                result = []
            if len(result) != len(chunk) * 2:
                result = ["??", "??:0"] * len(chunk)
            for index, address in enumerate(chunk):
                function = result[index * 2].strip()
                source = result[index * 2 + 1].strip()
                if function == "??":
                    function = f"{Path(path).name}+0x{address:x}"
                elif source not in {"??:0", "??:?"}:
                    function = f"{function} ({Path(source).name})"
                symbols[(path, address)] = function.replace(";", ":")
    return symbols


def write_folded(path: Path, values: collections.Counter[str]) -> None:
    path.write_text(
        "".join(
            f"{stack} {weight}\n"
            for stack, weight in sorted(
                values.items(), key=lambda item: (-item[1], item[0])
            )
        )
    )


def main() -> None:
    args = parse_args()
    header, records = read_records(args.input)
    if header["pid"] != args.pid:
        raise RuntimeError(
            f"allocation stack pid {header['pid']} does not match target {args.pid}"
        )
    starts, mappings = read_maps(args.pid, args.maps_output)
    resolved_records = []
    locations = set()
    for record in records:
        stack_locations = []
        for raw_address in record["frames"]:
            location = resolve_address(int(raw_address), starts, mappings)
            if location is not None:
                locations.add(location)
                stack_locations.append(location)
        resolved_records.append((record, stack_locations))
    symbols = symbolize(args.pid, locations)

    calls: collections.Counter[str] = collections.Counter()
    sampled_bytes: collections.Counter[str] = collections.Counter()
    leaves: collections.Counter[str] = collections.Counter()
    unresolved = 0
    for record, locations_for_record in resolved_records:
        frames = [symbols.get(location, "[unknown]") for location in locations_for_record]
        frames = [frame for frame in frames if not is_profiler_frame(frame)]
        frames.reverse()
        if not frames:
            unresolved += 1
            continue
        stack = ";".join(frames)
        calls[stack] += 1
        sampled_bytes[stack] += int(record["usable_size"])
        leaves[frames[-1]] += 1
    if not calls:
        raise RuntimeError("allocation stack profile contains no symbolized samples")

    write_folded(args.folded, calls)
    write_folded(args.bytes_folded, sampled_bytes)
    summary = {
        "schema_version": 1,
        "profiler": "allocator-neutral-sampled-call-stacks",
        "pid": args.pid,
        "sample_every": header["sample_every"],
        "generation": header["generation"],
        "sample_count": sum(calls.values()),
        "unresolved_sample_count": unresolved,
        "unique_stack_count": len(calls),
        "sampled_usable_bytes": sum(sampled_bytes.values()),
        "top_leaf_functions": [
            {"function": function, "samples": samples}
            for function, samples in leaves.most_common(50)
        ],
        "raw_input": str(args.input),
        "maps": str(args.maps_output),
        "folded": str(args.folded),
        "bytes_folded": str(args.bytes_folded),
    }
    args.summary.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
