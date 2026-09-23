#!/usr/bin/env python3
"""Preserve the target's vDSO, then match it to the recorded build ID."""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from collect_perf_debug import elf_info


def vdso_range(maps):
    for line in maps.splitlines():
        fields = line.split()
        if len(fields) != 6 or fields[5] != "[vdso]":
            continue
        start, end = (int(value, 16) for value in fields[0].split("-"))
        if "x" not in fields[1] or not 0 < end - start <= 16 * 1024 * 1024:
            raise ValueError("Invalid executable vDSO mapping")
        return start, end
    raise ValueError("Target has no vDSO mapping")


def save_report(root, report):
    (root / ".vdso.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def capture(pid, root, proc=Path("/proc")):
    root.mkdir(parents=True, exist_ok=True)
    target = root / "[vdso]"
    target.unlink(missing_ok=True)
    report = {"status": "unavailable", "pid": pid}
    try:
        start, end = vdso_range((proc / str(pid) / "maps").read_text())
        with (proc / str(pid) / "mem").open("rb", buffering=0) as memory:
            data = os.pread(memory.fileno(), end - start, start)
        if len(data) != end - start or not data.startswith(b"\x7fELF"):
            raise ValueError("Incomplete or non-ELF vDSO memory image")
        with tempfile.TemporaryDirectory(prefix=".vdso-", dir=root) as directory:
            image = Path(directory) / "vdso.so"
            image.write_bytes(data)
            build_id = elf_info(image)["build_id"]
            if not build_id:
                raise ValueError("Captured vDSO has no build ID")
            image.replace(target)
        report.update(status="captured", build_id=build_id,
                      address=hex(start), size=len(data))
    except (OSError, ValueError) as error:
        target.unlink(missing_ok=True)
        report["error"] = str(error)
    return save_report(root, report)


def verify(root, buildids):
    target = root / "[vdso]"
    try:
        report = json.loads((root / ".vdso.json").read_text())
        expected = set()
        for line in buildids.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[-1] == "[vdso]":
                if not re.fullmatch(r"[0-9a-fA-F]{8,}", fields[0]):
                    raise ValueError("Invalid recorded vDSO build ID")
                expected.add(fields[0].lower())
        if report.get("status") not in ("captured", "verified", "not-sampled"):
            target.unlink(missing_ok=True)
            return report
        actual = elf_info(target)["build_id"]
        if not actual or actual != report.get("build_id"):
            raise ValueError("Captured vDSO changed after collection")
        if expected and expected != {actual}:
            raise ValueError(f"vDSO build ID mismatch: captured {actual}, recorded {sorted(expected)}")
        report.update(status="verified" if expected else "not-sampled",
                      recorded_build_ids=sorted(expected))
    except (OSError, ValueError, KeyError, TypeError) as error:
        target.unlink(missing_ok=True)
        report = {"status": "unavailable", "error": str(error)}
    return save_report(root, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pid", type=int)
    action.add_argument("--buildids", type=Path)
    args = parser.parse_args()
    report = (capture(args.pid, args.root) if args.pid is not None
              else verify(args.root, args.buildids))
    print(json.dumps(report, indent=2))
    if report["status"] == "unavailable":
        print(f"WARNING: vDSO unavailable: {report.get('error', 'capture failed')}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
