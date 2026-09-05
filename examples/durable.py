#!/usr/bin/env python3
"""CPU profiling for the opt-in Temporal endpoint and DurableCall path."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dependency_command
from run import acquire_tooling_lock, build_profiler_image


HERE = Path(__file__).resolve().parent
ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", HERE.parent.parent)
).expanduser().resolve()
ARTIFACTS = HERE / ".artifacts" / "durable"
SCHEDULE_ID = "example-automation-schedule"
CALLS_RE = re.compile(r"(?:^|\n)calls:\s*(\d+)")


@dataclass(frozen=True)
class Language:
    name: str
    example: Path
    runtime: Path
    override_target: str
    tool: str
    process: str

    @property
    def project(self) -> str:
        return f"servicelib-durable-profiling-{self.name}"


LANGUAGES = {
    item.name: item
    for item in (
        Language(
            "go", ROOT / "goexample", ROOT / "servicelib",
            "/app/config/overrides.yaml", "perf", "service",
        ),
        Language(
            "python", ROOT / "pyexample", ROOT / "pyservicelib",
            "/workspace/config/docker_overrides.yaml", "pyspy",
            "automation_service.main",
        ),
        Language(
            "typescript", ROOT / "tsexample", ROOT / "tsservicelib",
            "/app/config/docker_overrides.yaml", "node-cpu",
            "http://automationservice:9229",
        ),
    )
}


def run(
    command: list[str], *, cwd: Path, env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("-", " ".join(command), flush=True)
    return subprocess.run(command, cwd=cwd, env=env, check=check, text=True)


def environment(language: Language, cores: int, duration: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DURABLE_PROFILING_ARTIFACTS": str(ARTIFACTS),
        "DURABLE_PROFILING_CORES": str(cores),
        "DURABLE_PROFILING_DIR": str(HERE),
        "DURABLE_PROFILING_DURATION": str(duration),
        "DOCKER_TARGET": "runtime",
        "RUNTIME_STRIP": "OFF",
    })
    if language.name == "go":
        env["GOSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    elif language.name == "python":
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    else:
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(language.runtime)
    return env


def prepare(language: Language) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    directory = ARTIFACTS / language.name
    directory.mkdir(parents=True, exist_ok=True)
    overrides = directory / "automationservice.overrides.yaml"
    overrides.write_text(
        """dataConnectors:
  temporal:
    address: temporal:7233
endpoints:
  durableJob:
    enabled: true
  localSchedule:
    enabled: false
  temporalSchedule:
    enabled: true
    schedule: "* * * * *"
    overlapPolicy: Allow
services:
  automationService:
    defaultGrpcTimeout: 0
    environment: ""
    grpcHost: 0.0.0.0
    grpcPort: 9204
    httpHost: 0.0.0.0
    httpPort: 9094
"""
    )
    node_options = (
        '      NODE_OPTIONS: "--inspect=0.0.0.0:9229 --enable-source-maps"\n'
        if language.name == "typescript" else ""
    )
    overlay = directory / "compose.yml"
    overlay.write_text(
        "services:\n"
        "  automationservice:\n"
        "    cpus: ${DURABLE_PROFILING_CORES}\n"
        "    environment:\n"
        "      SERVICELIB_NOOP_LOGS: ${PROFILING_NOOP_LOGS:-1}\n"
        "      SERVICELIB_NOOP_METRICS: ${PROFILING_NOOP_METRICS:-1}\n"
        "      SERVICELIB_NOOP_TRACING: ${PROFILING_NOOP_TRACING:-1}\n"
        f"{node_options}"
        "    volumes:\n"
        f"      - {overrides}:{language.override_target}:ro\n"
        "  profiler:\n"
        "    image: servicelib-profiler:local\n"
        "    pid: service:automationservice\n"
        "    cap_add: [SYS_PTRACE, SYS_ADMIN]\n"
        "    security_opt: [seccomp:unconfined]\n"
        "    profiles: [profiling]\n"
        "    environment:\n"
        # Temporal's Python SDK spends much of its time in Rust Core. Blocking
        # sampling is intentionally enabled only for this opt-in profile so
        # the Python activation/graph stacks are sampled often enough to be
        # useful. The normal profiler retains its low-overhead default.
        "      PROFILING_PYSPY_NONBLOCKING: \"0\"\n"
        "      PROFILING_PYSPY_RATE: \"100\"\n"
        "    volumes:\n"
        "      - ${DURABLE_PROFILING_ARTIFACTS}:/results\n"
        "    networks: [app_net]\n"
    )
    return overlay


def compose(language: Language, overlay: Path, *arguments: str) -> list[str]:
    command = [
        "docker", "compose", "--project-name", language.project,
        "--project-directory", str(language.example),
        "--file", str(language.example / "docker-compose.yml"),
    ]
    command += [
        part
        for runtime in sorted(language.example.glob("docker-compose.*-runtime.generated.yml"))
        for part in ("--file", str(runtime))
    ]
    return [*command, "--file", str(overlay), *arguments]


def build(language: Language, overlay: Path, env: dict[str, str]) -> None:
    build_profiler_image(env)
    if language.name == "go":
        dependency_command.run(
            ["make", "-C", "automationservice", "docker-build", f"PROJECT_DIR={language.example}"],
            cwd=language.example, env=env,
        )
    else:
        dependency_command.run(
            compose(language, overlay, "build", "automationservice"),
            cwd=language.example, env=env,
        )


def status() -> dict[str, object]:
    with urllib.request.urlopen("http://localhost:9094/status/data", timeout=3) as response:
        return json.loads(response.read())


def wait_ready(timeout: float = 90) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return status()
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last = error
            time.sleep(0.5)
    raise RuntimeError(f"Automation Service did not become ready: {last}")


def edge_calls(value: dict[str, object]) -> int:
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise RuntimeError("status graph has no nodes/edges")
    ids = {
        str(node.get("label", "")).split("(", 1)[0]: node.get("id")
        for node in nodes if isinstance(node, dict)
    }
    for edge in edges:
        if (
            isinstance(edge, dict)
            and edge.get("from") == ids.get("Consume Durable Job")
            and edge.get("to") == ids.get("Process Durable Job")
        ):
            match = CALLS_RE.search(str(edge.get("label", "")))
            return int(match.group(1)) if match else 0
    raise RuntimeError("status graph has no DurableCall edge")


def cli(language: Language, overlay: Path, env: dict[str, str], *arguments: str) -> None:
    run(
        compose(
            language, overlay, "run", "--rm", "--no-deps",
            "--entrypoint", "temporal", "temporal-create-namespace",
            *arguments, "--address", "temporal:7233", "--namespace", "default",
        ),
        cwd=language.example, env=env,
    )


def wait_file(path: Path, process: subprocess.Popen[str], timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"profiler exited before readiness with code {code}")
        time.sleep(0.2)
    raise RuntimeError("profiler did not become ready")


def profile_language(
    language: Language, *, cores: int, duration: int, jobs: int,
    skip_build: bool,
) -> dict[str, object]:
    overlay = prepare(language)
    env = environment(language, cores, duration)
    if not skip_build:
        build(language, overlay, env)
    down = compose(language, overlay, "down", "--volumes", "--remove-orphans")
    run(down, cwd=language.example, env=env, check=False)
    output = ARTIFACTS / f"{language.name}.automationservice.flamegraph.svg"
    ready = ARTIFACTS / f".{language.name}.automationservice.ready"
    output.unlink(missing_ok=True)
    ready.unlink(missing_ok=True)
    try:
        run(
            compose(
                language, overlay, "up", "--detach",
                "temporal-postgresql", "temporal-schema", "temporal",
                "temporal-create-namespace", "temporal-ui", "automationservice",
            ),
            cwd=language.example, env=env,
        )
        wait_ready()
        cli(
            language, overlay, env,
            "schedule", "toggle", "--schedule-id", SCHEDULE_ID, "--pause",
        )
        baseline = edge_calls(wait_ready())
        profiler = subprocess.Popen(
            compose(
                language, overlay, "--profile", "profiling", "run", "--rm",
                "--no-deps", "profiler", language.tool, language.process,
                str(duration), f"/results/{output.name}", f"/results/{ready.name}",
            ),
            cwd=language.example, env=env, text=True,
        )
        wait_file(ready, profiler)
        start = (
            datetime.now(timezone.utc).replace(second=1, microsecond=0)
            - timedelta(days=30)
        )
        cli(
            language, overlay, env,
            "schedule", "backfill", "--schedule-id", SCHEDULE_ID,
            "--start-time", start.isoformat().replace("+00:00", "Z"),
            "--end-time", (start + timedelta(minutes=jobs)).isoformat().replace("+00:00", "Z"),
            "--overlap-policy", "AllowAll",
        )
        timeout = max(180, duration * 16) if language.tool == "pyspy" else 120
        code = profiler.wait(timeout=timeout)
        if code != 0:
            raise RuntimeError(f"profiler exited with code {code}")
        completed = edge_calls(wait_ready()) - baseline
        required = [output, Path(f"{output}.folded.txt"), Path(f"{output}.top.txt")]
        missing = [path for path in required if not path.is_file() or path.stat().st_size == 0]
        if missing:
            raise RuntimeError(f"profiling artifacts are missing: {missing}")
        if language.tool == "pyspy":
            samples = sum(
                int(line.rsplit(" ", 1)[1])
                for line in Path(f"{output}.folded.txt").read_text().splitlines()
                if line.rsplit(" ", 1)[-1].isdigit()
            )
            minimum_samples = max(10, duration)
            if samples < minimum_samples:
                raise RuntimeError(
                    "Python DurableCall profile is not representative: "
                    f"captured {samples} samples, expected at least {minimum_samples}"
                )
        result = {
            "language": language.name,
            "cores": cores,
            "durationSeconds": duration,
            "submittedJobs": jobs,
            "completedJobs": completed,
            "profile": str(output),
        }
        (ARTIFACTS / f"{language.name}.summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
        return result
    finally:
        run(down, cwd=language.example, env=env, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES))
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--jobs", type=int, default=10_000)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if args.cores <= 0 or args.duration <= 0 or args.jobs <= 0:
        parser.error("--cores, --duration and --jobs must be positive")
    acquire_tooling_lock()
    results = [
        profile_language(
            LANGUAGES[name], cores=args.cores, duration=args.duration,
            jobs=args.jobs, skip_build=args.skip_build,
        )
        for name in (args.language or list(LANGUAGES))
    ]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n"
    )
    print(f"DurableCall profiling passed: {', '.join(args.language or LANGUAGES)}")
    print(f"Artifacts: {ARTIFACTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
