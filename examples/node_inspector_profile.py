#!/usr/bin/env python3
"""Capture Node CPU or sampling-heap profiles through the public Inspector API."""

from __future__ import annotations

import argparse
import json
import posixpath
import socket
import time
import urllib.request
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


RUNTIME_DIAGNOSTICS_KEY = "gorundebug.servicelib.profiling.runtime"
RUNTIME_SAMPLE_INTERVAL_SECONDS = 0.25


def inspector_websocket(base_url: str) -> str:
    parsed_base = urlparse(base_url)
    if parsed_base.hostname is None:
        raise RuntimeError(f"invalid Node inspector URL: {base_url}")
    address = socket.gethostbyname(parsed_base.hostname)
    port = parsed_base.port or 80
    resolved_base = parsed_base._replace(netloc=f"{address}:{port}").geturl()
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{resolved_base}/json/list", timeout=2
            ) as response:
                targets = json.load(response)
            if targets:
                url = str(targets[0]["webSocketDebuggerUrl"])
                target = urlparse(url)
                return target._replace(netloc=f"{address}:{port}").geturl()
        except (OSError, ValueError, KeyError) as error:
            last_error = error
        time.sleep(0.1)
    raise RuntimeError(f"Node inspector at {base_url} is unavailable: {last_error}")


class Inspector:
    def __init__(self, url: str) -> None:
        import websocket

        self.socket = websocket.create_connection(
            url, timeout=10, origin="http://localhost"
        )
        self.next_id = 1

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self.call_stream(method, params=params)

    def call_stream(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        event_method: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        request_id = self.next_id
        self.next_id += 1
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.socket.send(json.dumps(request))
        while True:
            response = json.loads(self.socket.recv())
            if (
                event_method is not None
                and response.get("method") == event_method
                and on_event is not None
            ):
                on_event(response.get("params", {}))
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"{method} failed: {response['error']}")
            return response.get("result", {})


def take_heap_snapshot(inspector: Inspector, output: Path) -> None:
    """Write a post-load retained-heap snapshot using the public Inspector API."""
    inspector.call("HeapProfiler.collectGarbage")
    with output.open("w") as stream:
        def write_chunk(params: dict[str, Any]) -> None:
            chunk = params.get("chunk")
            if not isinstance(chunk, str):
                raise RuntimeError("HeapProfiler.addHeapSnapshotChunk has no chunk")
            stream.write(chunk)

        inspector.call_stream(
            "HeapProfiler.takeHeapSnapshot",
            params={"reportProgress": False, "captureNumericValue": True},
            event_method="HeapProfiler.addHeapSnapshotChunk",
            on_event=write_chunk,
        )
    if output.stat().st_size == 0:
        raise RuntimeError("Node heap snapshot is empty")


def evaluate(inspector: Inspector, expression: str) -> Any:
    result = inspector.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "silent": False},
    )
    if "exceptionDetails" in result:
        raise RuntimeError(f"Node runtime diagnostics failed: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


def start_runtime_diagnostics(inspector: Inspector) -> None:
    key = json.dumps(RUNTIME_DIAGNOSTICS_KEY)
    evaluate(
        inspector,
        f"""(() => {{
          const hooks = process.getBuiltinModule('node:perf_hooks');
          const histogram = hooks.monitorEventLoopDelay({{resolution: 10}});
          const gc = [];
          const observer = new hooks.PerformanceObserver((list) => {{
            for (const entry of list.getEntries()) {{
              gc.push({{
                durationSeconds: entry.duration / 1000,
                kind: String(entry.detail?.kind ?? 'unknown')
              }});
            }}
          }});
          histogram.enable();
          observer.observe({{entryTypes: ['gc']}});
          globalThis[Symbol.for({key})] = {{
            gc,
            histogram,
            hooks,
            observer,
            previousCpu: process.cpuUsage(),
            previousUtilization: hooks.performance.eventLoopUtilization()
          }};
          return true;
        }})()""",
    )


def sample_runtime_diagnostics(inspector: Inspector, elapsed_seconds: float) -> dict[str, Any]:
    key = json.dumps(RUNTIME_DIAGNOSTICS_KEY)
    value = evaluate(
        inspector,
        f"""(() => {{
          const state = globalThis[Symbol.for({key})];
          if (!state) throw new Error('runtime diagnostics are not started');
          const currentUtilization = state.hooks.performance.eventLoopUtilization();
          const utilization = state.hooks.performance.eventLoopUtilization(
            currentUtilization,
            state.previousUtilization
          );
          state.previousUtilization = currentUtilization;
          const cpu = process.cpuUsage(state.previousCpu);
          state.previousCpu = process.cpuUsage();
          const resources = process.getActiveResourcesInfo();
          const resourcesByType = {{}};
          for (const resource of resources) {{
            resourcesByType[resource] = (resourcesByType[resource] ?? 0) + 1;
          }}
          const sample = {{
            activeResources: resources.length,
            activeResourcesByType: resourcesByType,
            cpuSystemSeconds: cpu.system / 1000000,
            cpuUserSeconds: cpu.user / 1000000,
            eventLoopActiveSeconds: utilization.active / 1000,
            eventLoopIdleSeconds: utilization.idle / 1000,
            eventLoopLagMaxSeconds: Number.isFinite(state.histogram.max)
              ? state.histogram.max / 1000000000
              : 0,
            eventLoopLagMeanSeconds: Number.isFinite(state.histogram.mean)
              ? state.histogram.mean / 1000000000
              : 0,
            eventLoopUtilization: utilization.utilization,
            gc: state.gc.splice(0),
            memory: process.memoryUsage()
          }};
          state.histogram.reset();
          return sample;
        }})()""",
    )
    if not isinstance(value, dict):
        raise RuntimeError("Node runtime diagnostics returned a non-object sample")
    return {"elapsedSeconds": elapsed_seconds, **value}


def stop_runtime_diagnostics(inspector: Inspector) -> None:
    key = json.dumps(RUNTIME_DIAGNOSTICS_KEY)
    evaluate(
        inspector,
        f"""(() => {{
          const symbol = Symbol.for({key});
          const state = globalThis[symbol];
          if (!state) return false;
          state.histogram.disable();
          state.observer.disconnect();
          delete globalThis[symbol];
          return true;
        }})()""",
    )


def frame_name(frame: dict[str, Any]) -> str:
    function = str(frame.get("functionName") or "(anonymous)").replace(";", ":")
    url = str(frame.get("url") or "native").replace(";", ":")
    line = int(frame.get("lineNumber", -1)) + 1
    if url.startswith("file://"):
        url = url.removeprefix("file://")
    return f"{function} ({url}:{line})"


def cpu_folded(profile: dict[str, Any]) -> list[str]:
    nodes = {int(node["id"]): node for node in profile.get("nodes", [])}
    parents: dict[int, int] = {}
    for node in nodes.values():
        for child in node.get("children", []):
            parents[int(child)] = int(node["id"])

    lines: list[str] = []
    samples = profile.get("samples", [])
    deltas = profile.get("timeDeltas", [])
    for index, sample in enumerate(samples):
        node_id = int(sample)
        stack: list[str] = []
        seen: set[int] = set()
        while node_id in nodes and node_id not in seen:
            seen.add(node_id)
            stack.append(frame_name(nodes[node_id].get("callFrame", {})))
            node_id = parents.get(node_id, 0)
        stack.reverse()
        if stack:
            weight = max(1, int(deltas[index]) if index < len(deltas) else 1)
            lines.append(f"{';'.join(stack)} {weight}")
    return lines


def heap_folded(profile: dict[str, Any]) -> list[str]:
    parents: dict[int, int] = {}
    nodes: dict[int, dict[str, Any]] = {}

    def visit(node: dict[str, Any], parent: int | None = None) -> None:
        node_id = int(node["id"])
        nodes[node_id] = node
        if parent is not None:
            parents[node_id] = parent
        for child in node.get("children", []):
            visit(child, node_id)

    visit(profile["head"])
    lines: list[str] = []
    for sample in profile.get("samples", []):
        node_id = int(sample["nodeId"])
        stack: list[str] = []
        seen: set[int] = set()
        while node_id in nodes and node_id not in seen:
            seen.add(node_id)
            stack.append(frame_name(nodes[node_id].get("callFrame", {})))
            node_id = parents.get(node_id, 0)
        stack.reverse()
        if stack:
            lines.append(f"{';'.join(stack)} {max(1, int(sample['size']))}")
    return lines


def source_map_text(inspector: Inspector, javascript_url: str) -> str | None:
    if not javascript_url.startswith("file://"):
        return None
    path = javascript_url.removeprefix("file://") + ".map"
    expression = (
        "process.getBuiltinModule('fs').readFileSync("
        + json.dumps(path)
        + ", 'utf8')"
    )
    result = inspector.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "silent": True},
    )
    if "exceptionDetails" in result:
        return None
    value = result.get("result", {}).get("value")
    return value if isinstance(value, str) else None


def apply_source_maps(profile: dict[str, Any], inspector: Inspector) -> None:
    """Rewrite generated JS locations to their TypeScript source locations."""
    from sourcemap import loads as load_source_map

    frames: list[dict[str, Any]] = []

    def collect_heap(node: dict[str, Any]) -> None:
        frames.append(node.get("callFrame", {}))
        for child in node.get("children", []):
            collect_heap(child)

    if "nodes" in profile:
        frames.extend(node.get("callFrame", {}) for node in profile["nodes"])
    elif "head" in profile:
        collect_heap(profile["head"])

    decoders: dict[str, Any | None] = {}
    for frame in frames:
        url = str(frame.get("url") or "")
        if url not in decoders:
            text = source_map_text(inspector, url)
            try:
                decoders[url] = load_source_map(text) if text else None
            except (TypeError, ValueError):
                decoders[url] = None
        decoder = decoders[url]
        if decoder is None:
            continue
        try:
            token = decoder.lookup(
                line=int(frame.get("lineNumber", 0)),
                column=int(frame.get("columnNumber", 0)),
            )
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        source = str(token.src)
        if url.startswith("file://") and not source.startswith(("file://", "/")):
            source = "file://" + posixpath.normpath(
                posixpath.join(posixpath.dirname(url.removeprefix("file://")), source)
            )
        frame["url"] = source
        frame["lineNumber"] = int(token.src_line)
        frame["columnNumber"] = int(token.src_col)
        if getattr(token, "name", None):
            frame["functionName"] = token.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("cpu", "heap"))
    parser.add_argument("inspector_url")
    parser.add_argument("duration", type=int)
    parser.add_argument("output")
    parser.add_argument("ready_file", nargs="?", default="")
    parser.add_argument("stop_file", nargs="?", default="")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = "cpuprofile" if args.mode == "cpu" else "heapprofile"
    raw_output = Path(f"{output}.{suffix}")
    folded_output = Path(f"{output}.folded.txt")
    runtime_output = Path(f"{output}.runtime.json")
    snapshot_output = Path(f"{output}.heapsnapshot")

    inspector = Inspector(inspector_websocket(args.inspector_url))
    runtime_samples: list[dict[str, Any]] = []
    try:
        inspector.call("Runtime.enable")
        start_runtime_diagnostics(inspector)
        if args.mode == "cpu":
            inspector.call("Profiler.enable")
            inspector.call("Profiler.setSamplingInterval", {"interval": 1000})
            inspector.call("Profiler.start")
        else:
            inspector.call("HeapProfiler.enable")
            inspector.call(
                "HeapProfiler.startSampling",
                {
                    "samplingInterval": 32768,
                    "includeObjectsCollectedByMajorGC": True,
                    "includeObjectsCollectedByMinorGC": True,
                },
            )
        if args.ready_file:
            ready = Path(args.ready_file)
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text("ready\n")
        started_at = time.monotonic()
        deadline = started_at + args.duration
        while time.monotonic() < deadline:
            if args.stop_file and Path(args.stop_file).is_file():
                break
            time.sleep(min(RUNTIME_SAMPLE_INTERVAL_SECONDS, deadline - time.monotonic()))
            runtime_samples.append(
                sample_runtime_diagnostics(inspector, time.monotonic() - started_at)
            )
        if args.mode == "cpu":
            profile = inspector.call("Profiler.stop")["profile"]
        else:
            profile = inspector.call("HeapProfiler.stopSampling")["profile"]
            take_heap_snapshot(inspector, snapshot_output)
        apply_source_maps(profile, inspector)
        lines = cpu_folded(profile) if args.mode == "cpu" else heap_folded(profile)
    finally:
        stop_runtime_diagnostics(inspector)
        inspector.close()

    raw_output.write_text(json.dumps(profile, indent=2) + "\n")
    folded_output.write_text("\n".join(lines) + ("\n" if lines else ""))
    runtime_output.write_text(
        json.dumps(
            {
                "durationSeconds": args.duration,
                "inspectorUrl": args.inspector_url,
                "mode": args.mode,
                "sampleIntervalSeconds": RUNTIME_SAMPLE_INTERVAL_SECONDS,
                "samples": runtime_samples,
            },
            indent=2,
        )
        + "\n"
    )
    if not lines:
        raise RuntimeError(f"Node {args.mode} profile contained no samples")
    if not runtime_samples:
        raise RuntimeError(f"Node {args.mode} profile contained no runtime samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
