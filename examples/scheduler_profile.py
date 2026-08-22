#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


def find_pid(pattern: str) -> int:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            except (OSError, UnicodeDecodeError):
                continue
            if pattern in command and "scheduler_profile.py" not in command:
                return int(entry.name)
        time.sleep(0.05)
    raise RuntimeError(f"process matching {pattern!r} not found")


def read_scheduler(pid: int) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for task in (Path("/proc") / str(pid) / "task").iterdir():
        try:
            tid = int(task.name)
            runtime, runqueue, slices = map(int, (task / "schedstat").read_text().split()[:3])
            status = (task / "status").read_text().splitlines()
            fields = dict(line.split(":", 1) for line in status if ":" in line)
            result[tid] = {
                "name": (task / "comm").read_text().strip(),
                "runtime_ns": runtime,
                "runqueue_wait_ns": runqueue,
                "timeslices": slices,
                "voluntary_context_switches": int(fields.get("voluntary_ctxt_switches", "0")),
                "involuntary_context_switches": int(fields.get("nonvoluntary_ctxt_switches", "0")),
            }
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            continue
    return result


def sample_wait_channels(pid: int) -> tuple[Counter[str], Counter[str]]:
    states: Counter[str] = Counter()
    wait_channels: Counter[str] = Counter()
    task_root = Path("/proc") / str(pid) / "task"
    try:
        tasks = list(task_root.iterdir())
    except FileNotFoundError:
        return states, wait_channels
    for task in tasks:
        try:
            stat = (task / "stat").read_text()
            close = stat.rfind(")")
            state = stat[close + 2 :].split(" ", 1)[0]
            states[state] += 1
            if state != "R":
                channel = (task / "wchan").read_text().strip() or "unknown"
                wait_channels[channel] += 1
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
    return states, wait_channels


def delta(before: dict[str, Any], after: dict[str, Any], field: str) -> int:
    return max(0, int(after[field]) - int(before[field]))


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit("usage: scheduler_profile.py PATTERN DURATION_SECONDS OUTPUT READY")
    pattern, duration_text, output_text, ready_text = sys.argv[1:]
    duration = float(duration_text)
    pid = find_pid(pattern)
    before = read_scheduler(pid)
    Path(ready_text).write_text(f"{pid}\n")
    state_samples: Counter[str] = Counter()
    wait_samples: Counter[str] = Counter()
    sample_count = 0
    started = time.monotonic_ns()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        states, waits = sample_wait_channels(pid)
        state_samples.update(states)
        wait_samples.update(waits)
        sample_count += 1
        time.sleep(0.02)
    ended = time.monotonic_ns()
    after = read_scheduler(pid)
    threads = []
    for tid in sorted(before.keys() & after.keys()):
        threads.append(
            {
                "tid": tid,
                "name": after[tid]["name"],
                "runtime_ns": delta(before[tid], after[tid], "runtime_ns"),
                "runqueue_wait_ns": delta(before[tid], after[tid], "runqueue_wait_ns"),
                "timeslices": delta(before[tid], after[tid], "timeslices"),
                "voluntary_context_switches": delta(
                    before[tid], after[tid], "voluntary_context_switches"
                ),
                "involuntary_context_switches": delta(
                    before[tid], after[tid], "involuntary_context_switches"
                ),
            }
        )
    output = {
        "schema_version": 1,
        "profiler": "procfs-schedstat-wchan",
        "pid": pid,
        "duration_ns": ended - started,
        "sample_interval_ms": 20,
        "sample_count": sample_count,
        "threads": threads,
        "totals": {
            key: sum(int(thread[key]) for thread in threads)
            for key in (
                "runtime_ns",
                "runqueue_wait_ns",
                "timeslices",
                "voluntary_context_switches",
                "involuntary_context_switches",
            )
        },
        "thread_state_samples": dict(state_samples.most_common()),
        "wait_channel_samples": dict(wait_samples.most_common()),
    }
    Path(output_text).write_text(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
