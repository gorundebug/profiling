#!/usr/bin/env python3
"""Bind kernel-provided ARM64 PAC metadata to one perf recording."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MASK_ENV = "PERF_ARM64_PAC_MASK"


def mask_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", value):
        raise ValueError("Invalid captured instruction PAC mask")
    return hex(int(value, 16))


def process_identity(pid, proc=Path("/proc")):
    directory = proc / str(pid)
    # The parenthesized comm field can itself contain spaces or ')'.
    fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
    header = (directory / "exe").open("rb")
    with header:
        elf = header.read(20)
    if len(elf) != 20 or elf[:4] != b"\x7fELF" or elf[5] not in (1, 2):
        raise ValueError("Cannot identify target ELF architecture")
    return {
        "pid": pid,
        "start_time": fields[19],
        "boot_id": (proc / "sys/kernel/random/boot_id").read_text().strip(),
        "elf_machine": int.from_bytes(elf[18:20], "little" if elf[5] == 1 else "big"),
    }


def capture(pid):
    report = {"version": 1, "status": "unavailable", "pid": pid}
    try:
        identity = process_identity(pid)
        report.update(identity)
        if identity["elf_machine"] != 183:
            report["status"] = "not-applicable"
            return report
        result = subprocess.run(["/usr/local/bin/capture-pac-mask", str(pid)],
                                capture_output=True, text=True, timeout=10, check=True)
        if process_identity(pid) != identity:
            raise ValueError("Target identity changed during PAC capture")
        value = result.stdout.strip()
        if value == "unsupported":
            report["status"] = "not-supported"
        else:
            report.update(status="captured", instruction_mask=mask_value(value))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None)
        report["error"] = (detail.strip() if isinstance(detail, str) and detail.strip()
                           else str(error))
    return report


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bind(report, pid, data):
    if report.get("version") != 1 or report.get("pid") != pid:
        raise ValueError("PAC metadata does not identify the recorded process")
    if report.get("status") == "captured":
        after = capture(pid)
        keys = ("pid", "start_time", "boot_id", "elf_machine", "instruction_mask")
        if after.get("status") != "captured" or any(after.get(k) != report.get(k) for k in keys):
            report = dict(report, status="unavailable", error="PAC state could not be confirmed after sampling")
        else:
            report = dict(report, status="verified")
    return dict(report, perf_sha256=digest(data))


def decode_environment(report, data, environment):
    if report.get("version") != 1 or report.get("perf_sha256") != digest(data):
        raise ValueError("PAC metadata does not match this perf recording")
    result = dict(environment)
    result.pop(MASK_ENV, None)
    if report.get("status") == "verified":
        if report.get("elf_machine") != 183:
            raise ValueError("PAC metadata architecture is not AArch64")
        result[MASK_ENV] = mask_value(report.get("instruction_mask"))
    elif report.get("status") not in ("not-applicable", "not-supported", "unavailable"):
        raise ValueError("PAC metadata was not finalized after recording")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "bind", "decode"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    args, command = parser.parse_known_args()
    try:
        if args.action == "capture":
            if args.pid is None or args.pid <= 0 or command:
                raise ValueError("capture requires a positive PID and no command")
            report = capture(args.pid)
        else:
            if args.data is None:
                raise ValueError("bind/decode requires --data")
            report = json.loads(args.metadata.read_text())
            if args.action == "bind":
                if args.pid is None or command:
                    raise ValueError("bind requires --pid and no command")
                report = bind(report, args.pid, args.data)
            else:
                if not command or command[0] != "--" or len(command) < 2:
                    raise ValueError("decode requires -- followed by the perf command")
                environment = decode_environment(report, args.data, os.environ)
                if report["status"] == "unavailable":
                    print("WARNING: PAC metadata unavailable; signed callers may remain unknown", file=sys.stderr)
                os.execvpe(command[1], command[1:], environment)
        args.metadata.write_text(json.dumps(report, indent=2) + "\n")
        if report["status"] == "unavailable":
            print(f"WARNING: PAC capture unavailable: {report.get('error', 'unknown cause')}", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"PAC metadata error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
