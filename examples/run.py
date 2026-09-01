#!/usr/bin/env python3

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILING_DIR = Path(__file__).resolve().parent
PROFILING_ROOT = PROFILING_DIR.parent
ROOT = Path(
    os.environ.get(
        "DEPENDENCIES_DIR",
        str(PROFILING_ROOT.parent),
    )
).expanduser().resolve()
ARTIFACTS = PROFILING_DIR / ".artifacts"
COMMON_COMPOSE = PROFILING_DIR / "compose.common.yml"
USERVER_REMOTE_CONTEXT = (
    "https://github.com/userver-framework/userver.git"
    "#c9f77729c0edce7e423def2d4a4450aa7fc9d259"
)
TOOLING_LOCK_ENV = "SERVICELIB_TOOLING_LOCK_HELD"


def cppboost_dependency_context(dependency: str) -> str:
    versions = ROOT / "cppboostservicelib" / "cmake" / "DependencyVersions.cmake"
    try:
        contents = versions.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"cannot read pinned C++ dependency versions from {versions}: {error}"
        ) from error
    repositories = {
        "grpc": "https://github.com/grpc/grpc.git",
        "asio-grpc": "https://github.com/Tradias/asio-grpc.git",
    }
    repository = repositories.get(dependency)
    if repository is None:
        raise RuntimeError(f"unsupported Boost dependency context: {dependency}")
    prefix = f"CPPBOOSTSERVICELIB_{dependency.upper().replace('-', '_')}"
    match = re.search(
        rf'^set\({re.escape(prefix)}_VERSION "([^"]+)"',
        contents,
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError(f"{prefix}_VERSION is missing from {versions}")
    return f"{repository}#{match.group(1)}"


def acquire_tooling_lock() -> None:
    if os.environ.get(TOOLING_LOCK_ENV) == "1":
        return
    lock = Path(os.environ.get("TMPDIR", "/tmp")) / "servicelib-tooling.lock"

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    for _ in range(2):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                owner = int((lock / "pid").read_text().strip())
            except (OSError, ValueError):
                owner = 0
            if owner and alive(owner):
                raise RuntimeError(
                    "another ServiceLib benchmark/profiling/conformance run is active "
                    f"(pid {owner}); run these tools sequentially"
                )
            try:
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass
    else:
        raise RuntimeError(
            f"ServiceLib tooling lock is busy: {lock}; "
            "run benchmark, profiling and conformance sequentially"
        )

    (lock / "pid").write_text(f"{os.getpid()}\n")
    os.environ[TOOLING_LOCK_ENV] = "1"

    def release() -> None:
        try:
            (lock / "pid").unlink(missing_ok=True)
            lock.rmdir()
        except OSError:
            pass

    atexit.register(release)


@dataclass(frozen=True)
class Language:
    name: str
    example: Path
    overlay: Path
    # Tool used to sample the target process.
    tool: str
    # Patterns passed to `pgrep -f` inside the profiler container (which
    # shares the target service's PID namespace) to find the process.
    order_process_pattern: str
    inventory_process_pattern: str
    analytics_process_pattern: str | None = None
    repository: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    expected_outcome: str = "success"
    expected_business_status: str = ""
    mode: str = "closed"
    request_timeout: str = "60s"
    graceful_stop_seconds: int = 5
    kafka_enabled: bool = False
    pause_inventory: bool = False


SCENARIOS = {
    scenario.key: scenario
    for scenario in (
        Scenario("normal", "process_order_out_of_stock"),
        Scenario(
            "timeout",
            "process_order_timeout",
            expected_business_status="TIMED_OUT",
            pause_inventory=True,
        ),
        Scenario(
            "cancellation",
            "process_order_cancellation",
            expected_outcome="transport-timeout",
            request_timeout="100ms",
            graceful_stop_seconds=1,
            pause_inventory=True,
        ),
        Scenario(
            "overload",
            "process_order_overload",
            expected_outcome="overload",
            mode="arrival-rate",
        ),
        Scenario(
            "kafka-recovery",
            "process_order_kafka_recovery",
            kafka_enabled=True,
        ),
    )
}


LANGUAGES = (
    Language("go", ROOT / "goexample", PROFILING_DIR / "compose.go.yml", "perf", "service", "service"),
    Language(
        "go-native", ROOT / "gonativeexample",
        PROFILING_DIR / "compose.go-native.yml", "perf",
        "orderservice", "inventoryservice",
        repository="https://github.com/gorundebug/gonativeexample.git",
        revision="v0.2.59",
    ),
    Language("cpp", ROOT / "cppexample", PROFILING_DIR / "compose.cpp.yml", "perf", "example_order_service", "example_inventory_service"),
    Language(
        "cpp-native", ROOT / "cppnativeexample",
        PROFILING_DIR / "compose.cpp-native.yml", "perf",
        "orderservice", "inventoryservice",
        repository="https://github.com/gorundebug/cppnativeexample.git",
        revision="v0.2.59",
    ),
    Language("cppboost", ROOT / "cppboostexample", PROFILING_DIR / "compose.cppboost.yml", "perf", "example_order_service", "example_inventory_service"),
    Language(
        "cppboost-native", ROOT / "cppboostnativeexample",
        PROFILING_DIR / "compose.cppboost-native.yml", "perf",
        "orderservice", "inventoryservice",
        repository="https://github.com/gorundebug/cppboostnativeexample.git",
        revision="v0.2.59",
    ),
    Language("python", ROOT / "pyexample", PROFILING_DIR / "compose.python.yml", "pyspy", "order_service.main", "inventory_service.main"),
    Language(
        "python-native", ROOT / "pynativeexample",
        PROFILING_DIR / "compose.python-native.yml", "pyspy",
        "order_service.py", "inventory_service.py",
        repository="https://github.com/gorundebug/pynativeexample.git",
        revision="v0.2.59",
    ),
    Language("rust", ROOT / "rustexample", PROFILING_DIR / "compose.rust.yml", "perf", "service", "service"),
    Language(
        "rust-native", ROOT / "rustnativeexample",
        PROFILING_DIR / "compose.rust-native.yml", "perf", "service", "service",
        repository="https://github.com/gorundebug/rustnativeexample.git",
        revision="v0.2.59",
    ),
    Language(
        "typescript",
        ROOT / "tsexample",
        PROFILING_DIR / "compose.typescript.yml",
        "node-cpu",
        "main.generated.js",
        "main.generated.js",
        "main.generated.js",
    ),
    Language(
        "typescript-native",
        ROOT / "tsnativeexample",
        PROFILING_DIR / "compose.typescript-native.yml",
        "node-cpu",
        "dist/src/orders/main.js",
        "dist/src/inventory/main.js",
        "dist/src/analytics/main.js",
        repository="https://github.com/gorundebug/tsnativeexample.git",
        revision="v0.2.59",
    ),
)

# Each language profiled twice, sequentially: once sharing orderservice's PID
# namespace, once sharing inventoryservice's. Sequential (not concurrent)
# because a single profiler container can only share one target's PID
# namespace at a time, and running both `perf`/`py-spy` at once against the
# same CPU quota would also distort each other's samples.
TARGETS = (
    ("orderservice", "profiler"),
    ("inventoryservice", "profiler-inventory"),
)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def popen(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    print("+", " ".join(command), "&", flush=True)
    return subprocess.Popen(command, cwd=cwd, env=env, text=True)


def ensure_example(language: Language, env: dict[str, str]) -> None:
    compose_file = language.example / "docker-compose.yml"
    if compose_file.is_file():
        if (
            language.repository is not None
            and language.revision is not None
            and env.get("UPDATE_MANAGED_DEPENDENCIES") == "1"
        ):
            status = run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=language.example,
                env=env,
                capture=True,
            )
            if status.stdout.strip():
                raise RuntimeError(
                    f"managed {language.name} checkout has local changes; "
                    "refusing to update"
                )
            run(
                [
                    "git", "fetch", "--depth", "1", "origin",
                    f"refs/tags/{language.revision}:refs/tags/{language.revision}",
                ],
                cwd=language.example,
                env=env,
            )
            head = run(
                ["git", "rev-parse", "HEAD"], cwd=language.example,
                env=env, capture=True,
            ).stdout.strip()
            pinned = run(
                ["git", "rev-list", "-n", "1", language.revision],
                cwd=language.example, env=env, capture=True,
            ).stdout.strip()
            if head != pinned:
                print(f"Updating {language.name} to {language.revision}", flush=True)
                run(
                    ["git", "checkout", "--detach", language.revision],
                    cwd=language.example,
                    env=env,
                )
        return
    if language.example.exists():
        raise RuntimeError(
            f"{language.name} example exists at {language.example}, but "
            f"{compose_file.name} is missing; refusing to replace it"
        )
    if language.repository is None or language.revision is None:
        raise RuntimeError(
            f"{language.name} example is missing at {language.example} and "
            "has no configured repository"
        )

    language.example.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Fetching {language.name} {language.revision} from "
        f"{language.repository}",
        flush=True,
    )
    with tempfile.TemporaryDirectory(
        prefix=f".{language.example.name}-clone-",
        dir=language.example.parent,
    ) as temporary_directory:
        checkout = Path(temporary_directory) / language.example.name
        run(
            [
                "git", "clone", "--branch", language.revision,
                "--depth", "1", language.repository, str(checkout),
            ],
            cwd=language.example.parent,
            env=env,
        )
        if not (checkout / "docker-compose.yml").is_file():
            raise RuntimeError(
                f"downloaded {language.name} does not contain docker-compose.yml"
            )
        checkout.rename(language.example)


def compose_command(language: Language, *args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"servicelib-example-profiling-{language.name}",
        "--project-directory",
        str(language.example),
        "--file",
        str(language.example / "docker-compose.yml"),
    ]
    for runtime_overlay in sorted(
        language.example.glob("docker-compose.*-runtime.generated.yml")
    ):
        command.extend(["--file", str(runtime_overlay)])
    command.extend([
        "--file", str(COMMON_COMPOSE),
        "--file", str(language.overlay),
        *args,
    ])
    return command


def environment(args: argparse.Namespace, language: Language) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PROFILING_ARTIFACTS_DIR": str(ARTIFACTS),
            "PROFILING_ALLOCATOR_LIBRARY": str(
                ARTIFACTS / "liballocation_profile.so"
            ),
            "PROFILING_CPPBOOST_CONFIG_DIR": str(ARTIFACTS / "cppboost-config"),
            "PROFILING_CPP_CONFIG_DIR": str(ARTIFACTS / "cpp-config"),
            "PROFILING_DIR": str(PROFILING_DIR),
            "PROFILING_DURATION": args.duration,
            "PROFILING_LOADGEN_CORES": str(args.loadgen_cores),
            "PROFILING_RESULT_FILE": "/results/unused.json",
            "PROFILING_SERVICE_CORES": str(args.cores),
            "PROFILING_VUS": str(args.vus),
            "DOCKER_TARGET": "runtime",
            "EXAMPLE_PROFILE": getattr(
                args, "graph_profile", "function-call"
            ),
            "RUNTIME_STRIP": "OFF",
        }
    )
    apply_scenario_environment(
        env,
        SCENARIOS[getattr(args, "scenario", "normal")],
        getattr(args, "rate", 100_000),
    )
    if language.name == "cpp":
        env["COMPOSE_PROJECT_NAME"] = "cppexample"
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppservicelib")
        env["USERVER_LTO"] = "ON"
    elif language.name == "cpp-native":
        local_userver = ROOT / "userver"
        env["USERVER_SOURCE_CONTEXT"] = os.environ.get("USERVER_SOURCE_CONTEXT") or (
            str(local_userver)
            if local_userver.is_dir()
            else USERVER_REMOTE_CONTEXT
        )
        env["USERVER_LTO"] = "ON"
    elif language.name == "cppboost":
        env["COMPOSE_PROJECT_NAME"] = "cppboostexample"
        env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        if getattr(args, "coroutine_diagnostics", False):
            env["CPPBOOSTSERVICELIB_PROFILING"] = "ON"
            env["CPPBOOSTSERVICELIB_COROUTINE_DIAGNOSTICS"] = "ON"
    if language.name in {"cppboost", "cppboost-native"}:
        # Never let generated Compose fall back to the example checkout for
        # these named contexts. A populated build tree can contain matching
        # gRPC headers, causing CMake to configure the example itself as gRPC.
        env.setdefault(
            "GRPC_SOURCE_CONTEXT",
            cppboost_dependency_context("grpc"),
        )
        env.setdefault(
            "ASIO_GRPC_SOURCE_CONTEXT",
            cppboost_dependency_context("asio-grpc"),
        )
    elif language.name == "python":
        env["PROFILING_PYTHON_CONFIG_DIR"] = str(ARTIFACTS / "python-config")
        env["PYSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "pyservicelib")
    elif language.name == "typescript":
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "tsservicelib")
    return env


def apply_scenario_environment(
    env: dict[str, str], scenario: Scenario, rate: int
) -> None:
    env.update(
        {
            "PROFILING_EXPECTED_BUSINESS_STATUS":
                scenario.expected_business_status,
            "PROFILING_EXPECTED_OUTCOME": scenario.expected_outcome,
            "PROFILING_GRACEFUL_STOP": f"{scenario.graceful_stop_seconds}s",
            "PROFILING_MODE": scenario.mode,
            "PROFILING_ORDER_PROCESSED_ENABLED":
                "true" if scenario.kafka_enabled else "false",
            "PROFILING_RATE": str(rate if scenario.mode == "arrival-rate" else 0),
            "PROFILING_REQUEST_TIMEOUT": scenario.request_timeout,
            "PROFILING_SCENARIO": scenario.name,
        }
    )


def scenario_artifact_name(args: argparse.Namespace, name: str) -> str:
    scenario = getattr(args, "scenario", "normal")
    if scenario == "normal":
        return name
    stem, suffix = os.path.splitext(name)
    return f"{stem}.{scenario}{suffix}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_source_identity(path: Path) -> dict[str, Any]:
    revision = run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        env=os.environ.copy(),
        capture=True,
    ).stdout.strip()
    dirty = run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=path,
        env=os.environ.copy(),
        capture=True,
    ).stdout.strip()
    return {"path": str(path), "revision": revision, "dirty": bool(dirty)}


def image_identity(image_name: str) -> dict[str, str]:
    result = run(
        [
            "docker", "image", "inspect", "--format",
            "{{.Id}} {{json .RepoDigests}}", image_name,
        ],
        cwd=PROFILING_DIR,
        env=os.environ.copy(),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return {"name": image_name, "id": "unavailable", "digest": "unavailable"}
    image_id, _, raw_digests = result.stdout.strip().partition(" ")
    digests = json.loads(raw_digests or "[]") or []
    return {
        "name": image_name,
        "id": image_id,
        "digest": str(digests[0]) if digests else "unavailable",
    }


def write_run_manifest(
    args: argparse.Namespace, selected: list[Language]
) -> Path:
    scenario = SCENARIOS[args.scenario]
    sources: dict[str, dict[str, Any]] = {
        "profiling": git_source_identity(PROFILING_ROOT)
    }
    inputs = {
        str(path.relative_to(PROFILING_ROOT)): file_sha256(path)
        for path in (
            Path(__file__).resolve(),
            PROFILING_DIR / "load.js",
            COMMON_COMPOSE,
            *(language.overlay for language in selected),
        )
    }
    images: dict[str, dict[str, str]] = {
        "profiler": image_identity("servicelib-profiler:local")
    }
    for language in selected:
        sources[language.name] = git_source_identity(language.example)
        framework = {
            "typescript": ROOT / "tsservicelib",
        }.get(language.name)
        if framework is not None:
            sources[f"{language.name}-framework"] = git_source_identity(framework)
        resolved = run(
            compose_command(language, "config", "--images"),
            cwd=language.example,
            env=environment(args, language),
            capture=True,
        )
        for image_name in sorted(set(resolved.stdout.splitlines())):
            if image_name:
                images[f"{language.name}:{image_name}"] = image_identity(image_name)
    payload = {
        "schema_version": 1,
        "graph_profile": args.graph_profile,
        "scenario": {
            "key": scenario.key,
            "name": scenario.name,
            "expected_outcome": scenario.expected_outcome,
            "expected_business_status": scenario.expected_business_status,
            "request_timeout": scenario.request_timeout,
            "graceful_stop_seconds": scenario.graceful_stop_seconds,
            "kafka_enabled": scenario.kafka_enabled,
            "inventory_paused": scenario.pause_inventory,
        },
        "load": {
            "mode": scenario.mode,
            "rate": args.rate if scenario.mode == "arrival-rate" else 0,
            "vus": args.vus,
            "duration": args.duration,
            "warmup": args.warmup,
        },
        "quotas": {
            "service_cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
        },
        "profile_kinds": list(args.profile_kind),
        "sources": sources,
        "input_sha256": inputs,
        "images": images,
    }
    output = ARTIFACTS / scenario_artifact_name(args, "run-manifest.json")
    output.write_text(json.dumps(payload, indent=2) + "\n")
    return output


def build_profiler_image(env: dict[str, str]) -> None:
    registry = "docker.io"
    if env.get("DEPENDENCY_PROXY_DIR"):
        registry = (
            f"{env.get('DEPENDENCY_PROXY_HOST', 'localhost')}:"
            f"{env.get('DEPENDENCY_PROXY_DOCKER_PORT', '18083')}"
        )
    build_args: list[str] = [
        "--build-arg", f"DEPENDENCY_DOCKER_REGISTRY={registry}",
    ]
    if env.get("DEPENDENCY_PROXY_DIR"):
        build_args.extend([
            "--add-host", "host.docker.internal:host-gateway",
        ])
    for name in (
        "DEPENDENCY_APT_DEBIAN_URL",
        "DEPENDENCY_APT_DEBIAN_SECURITY_URL",
        "DEPENDENCY_GITHUB_RAW_URL",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
    ):
        if value := docker_build_environment_value(env, name):
            build_args.extend(["--build-arg", f"{name}={value}"])
    run(
        ["docker", "build", *build_args, "-f", "Dockerfile.profiler", "-t", "servicelib-profiler:local", "."],
        cwd=PROFILING_DIR,
        env=env,
    )


def docker_build_environment_value(env: dict[str, str], name: str) -> str | None:
    value = env.get(name)
    if not value or not env.get("DEPENDENCY_PROXY_DIR"):
        return value
    host = env.get("DEPENDENCY_PROXY_HOST", "localhost")
    docker_host = env.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    if name == "PIP_TRUSTED_HOST" and value == host:
        return docker_host
    return value.replace(f"://{host}:", f"://{docker_host}:")


def extract_profiler_assets(env: dict[str, str]) -> None:
    allocator = ARTIFACTS / "liballocation_profile.so"
    allocator.unlink(missing_ok=True)
    run(
        [
            "docker", "run", "--rm",
            "--volume", f"{ARTIFACTS}:/out",
            "--entrypoint", "cp",
            "servicelib-profiler:local",
            "/usr/local/lib/liballocation_profile.so",
            "/out/liballocation_profile.so",
        ],
        cwd=PROFILING_DIR,
        env=env,
    )
    if not allocator.is_file() or allocator.stat().st_size == 0:
        raise RuntimeError("profiler image did not export allocation profiler")


def build(language: Language, env: dict[str, str]) -> None:
    if language.name == "go":
        run(["make", "docker-build"], cwd=language.example, env=env)
    elif language.name in {"cpp", "cppboost"}:
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    elif language.name == "typescript":
        run(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env=env,
        )
    else:
        services = ["inventoryservice", "orderservice"]
        if env.get("PROFILING_ORDER_PROCESSED_ENABLED") == "true":
            services.insert(0, "analyticsservice")
        run(
            compose_command(language, "build", *services),
            cwd=language.example,
            env=env,
        )


def verify_cppboost_release_build(
    env: dict[str, str], *, require_coroutine_diagnostics: bool
) -> None:
    """Reject a stale or incorrectly instrumented runtime image."""

    def image_label(image: str, name: str) -> str:
        result = run(
            [
                "docker", "image", "inspect",
                "--format", f'{{{{ index .Config.Labels "{name}" }}}}',
                image,
            ],
            cwd=PROFILING_DIR,
            env=env,
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "cppboost runtime image is missing; re-run without --skip-build"
            )
        return result.stdout.strip()

    for service in ("inventoryservice", "orderservice"):
        image = f"cppboostexample-{service}"
        build_type = image_label(image, "org.gorundebug.build-type")
        if build_type != "Release":
            raise RuntimeError(
                "cppboost profiling requires Release runtime images; "
                f"{image} label reports {build_type or 'missing'}. "
                "Re-run without --skip-build."
            )
        if require_coroutine_diagnostics:
            diagnostics = image_label(
                image,
                "org.gorundebug.cppboostservicelib.coroutine-diagnostics",
            )
            if diagnostics != "ON":
                raise RuntimeError(
                    "cppboost coroutine diagnostics were requested, but "
                    f"{image} was built without diagnostics. "
                    "Re-run without --skip-build."
                )


# userver splits work across several independently-pooled task processors,
# unlike Go's single GOMAXPROCS-sized scheduler. Setting every pool's
# worker_threads to `cores` (as before) oversubscribes the cgroup CPU quota
# 3x over (main + fs + grpc-blocking, each sized at `cores`). For parity with
# Go: only main-task-processor (the CPU-bound pool that actually runs request
# handling fibers) gets `cores` threads, matching GOMAXPROCS exactly. The
# blocking/I/O-only pools (fs-task-processor, grpc-blocking-task-processor,
# and the gRPC server's completion queues) get a minimal fixed size, the same
# role Go's runtime fills with extra OS threads for blocking syscalls that
# don't count against GOMAXPROCS.
AUX_TASK_PROCESSOR_THREADS = 1


def _set_worker_threads(
    static_config: str,
    processor: str,
    threads: int,
    *,
    required: bool = True,
) -> str:
    import re

    pattern = re.compile(rf"(?m)^(\s*{re.escape(processor)}:\n\s+worker_threads:)\s+\d+\s*$")
    new_config, count = pattern.subn(rf"\1 {threads}", static_config)
    if count == 0 and not required:
        return static_config
    if count != 1:
        raise RuntimeError(
            f"expected exactly one worker_threads under {processor}, found {count}"
        )
    return new_config


def prepare_cpp_configs(service_cores: int) -> None:
    import re

    output = ARTIFACTS / "cpp-config"
    output.mkdir(parents=True, exist_ok=True)
    for service, prefix, port, grpc_port in (
        ("inventoryservice", "inventoryService", 9092, 9202),
        ("orderservice", "orderService", 9091, 9201),
    ):
        static_config = (
            ROOT / "cppexample" / service / "static_config.yaml"
        ).read_text()
        static_config = static_config.replace(
            "        tracing: otlp", "        tracing: default"
        )
        # "none" fully suppresses log output (userver logging::Level::kNone).
        # servicelib bridges its own logging through userver's LOG_* macros
        # too, so this also silences servicelib's warnings/errors -- fine
        # for profiling, where we only care about CPU cost under load.
        static_config = static_config.replace("level: info", "level: none")
        static_config = _set_worker_threads(static_config, "main-task-processor", service_cores)
        static_config = _set_worker_threads(
            static_config, "fs-task-processor", AUX_TASK_PROCESSOR_THREADS
        )
        static_config = _set_worker_threads(
            static_config,
            "grpc-blocking-task-processor",
            AUX_TASK_PROCESSOR_THREADS,
            required=False,
        )
        static_config, _ = re.subn(
            r"(?m)^(\s+completion-queue-count:)\s+\d+\s*$",
            rf"\1 {AUX_TASK_PROCESSOR_THREADS}",
            static_config,
        )
        (output / f"{service}.static_config.yaml").write_text(static_config)

        override_path = output / f"{service}.overrides.yaml"
        if service == "orderservice":
            override_path.write_text(
                "streams:\n"
                "  publishOrderProcessed:\n"
                "    enabled: false\n"
            )
        else:
            override_path.write_text("{}\n")

        values = {
            f"{prefix}ConfigOverridePath":
                f"/profiling-config/{service}.overrides.yaml",
            f"{prefix}DefaultGrpcTimeout": 0 if service == "inventoryservice" else 5000,
            f"{prefix}Environment": "",
            f"{prefix}GrpcHost": "0.0.0.0",
            f"{prefix}GrpcPort": grpc_port,
            f"{prefix}HttpHost": "0.0.0.0",
            f"{prefix}HttpPort": port,
            f"{prefix}OtlpEndpoint": "disabled:4317",
            "inventoryServiceApiAddress": "dns:///inventoryservice:9202",
            "inventoryServiceApiConnectionsCount": 1,
        }
        if service == "inventoryservice":
            values["inventoryPriorityWorkersExecutorsCount"] = service_cores
        else:
            values["defaultPoolExecutorsCount"] = service_cores
            values.update(disabled_kafka_connector_values())
            values["softDeadlineDuration"] = 1000
            values["orderProcessedEnabled"] = False
        text = "".join(
            f"{key}: {json.dumps(value)}\n"
            for key, value in values.items()
        )
        (output / f"{service}.config_vars.yaml").write_text(text)


def disabled_kafka_connector_values() -> dict[str, object]:
    """Keep the disabled endpoint's connector structurally valid."""
    return {
        "orderEventsBrokers": "redpanda:9092",
        "orderEventsPassword": "",
        "orderEventsSaslMechanism": "SCRAM-SHA-512",
        "orderEventsSecurityProtocol": "PLAINTEXT",
        "orderEventsUsername": "",
    }


def disable_order_processed_endpoint(values: str) -> str:
    endpoint = "  orderProcessed:\n    enabled: false\n"
    pattern = re.compile(
        r"(?m)^  orderProcessed:\n    enabled: (?:true|false)\n"
    )
    if pattern.search(values):
        values = pattern.sub(endpoint, values, count=1)
    elif "endpoints:\n" in values:
        values = values.replace("endpoints:\n", "endpoints:\n" + endpoint, 1)
    else:
        values = values + ("\n" if values and not values.endswith("\n") else "")
        values += "endpoints:\n" + endpoint
    if values.count(endpoint) != 1:
        raise RuntimeError(
            "orderservice values must disable exactly one orderProcessed endpoint"
        )
    return values


def prepare_cppboost_configs(service_cores: int) -> None:
    """Prepare Boost values files while preserving the generated schema."""
    output = ARTIFACTS / "cppboost-config"
    output.mkdir(parents=True, exist_ok=True)
    for service, pool in (
        ("inventoryservice", "inventoryPriorityWorkers"),
        ("orderservice", "defaultPool"),
    ):
        values = (
            ROOT / "cppboostexample" / service / "config" / "overrides.yaml"
        ).read_text()
        values = values.replace("connectionsCount: 1", f"connectionsCount: {service_cores}")
        values = values.replace("executorsCount: 2", f"executorsCount: {service_cores}")
        if service == "orderservice":
            values = disable_order_processed_endpoint(values)
        if f"  {pool}:" not in values:
            raise RuntimeError(f"missing canonical pool {pool} in {service} values")
        (output / f"{service}.overrides.yaml").write_text(values)


def prepare_python_configs() -> None:
    output = ARTIFACTS / "python-config"
    output.mkdir(parents=True, exist_ok=True)
    for service in ("inventoryservice", "orderservice"):
        values = (
            ROOT / "pyexample" / service / "config" / "docker_overrides.yaml"
        ).read_text()
        if service == "orderservice":
            values = disable_order_processed_endpoint(values)
        (output / f"{service}.overrides.yaml").write_text(values)


def prepare_selected_configs(
    selected: list[Language], service_cores: int
) -> None:
    selected_names = {language.name for language in selected}
    if "cpp" in selected_names:
        prepare_cpp_configs(service_cores)
    if "cppboost" in selected_names:
        prepare_cppboost_configs(service_cores)
    if "python" in selected_names:
        prepare_python_configs()


def wait_for_service(
    language: Language, service: str, url: str, env: dict[str, str]
) -> None:
    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    logs = run(
        compose_command(language, "logs", "--no-color", "--tail", "100", service),
        cwd=language.example,
        env=env,
        capture=True,
        check=False,
    )
    print(logs.stdout + logs.stderr, file=sys.stderr)
    raise RuntimeError(f"{language.name} {service} did not become ready: {last_error}")


def load(
    language: Language,
    env: dict[str, str],
    *,
    duration: str,
    result_name: str,
    runtime_metrics_url: str | None = None,
    runtime_metrics_name: str | None = None,
    orchestrate: bool = False,
    scenario_override: str | None = None,
) -> None:
    result_path = ARTIFACTS / result_name
    result_path.unlink(missing_ok=True)
    load_env = {
        **env,
        "PROFILING_DURATION": duration,
        "PROFILING_RESULT_FILE": f"/results/{result_name}",
    }
    scenario = SCENARIOS[
        scenario_override or load_env.get("PROFILING_SCENARIO_KEY", "normal")
    ]
    if scenario_override is None:
        scenario = next(
            item
            for item in SCENARIOS.values()
            if item.name == load_env["PROFILING_SCENARIO"]
        )
    apply_scenario_environment(
        load_env,
        scenario,
        int(load_env.get("PROFILING_RATE", "0") or "0") or 100_000,
    )
    command = compose_command(
        language,
        "--profile",
        "profiling",
        "run",
        "--rm",
        "--no-deps",
        "loadgen",
    )
    if runtime_metrics_url is None and not (orchestrate and scenario.kafka_enabled):
        run(command, cwd=language.example, env=load_env)
        validate_load_result(result_path, language, duration, load_env)
        return

    if runtime_metrics_url is not None and runtime_metrics_name is None:
        raise ValueError("runtime_metrics_name is required with runtime_metrics_url")
    metrics_path = ARTIFACTS / runtime_metrics_name if runtime_metrics_name else None
    if metrics_path is not None:
        metrics_path.unlink(missing_ok=True)
    samples: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    orchestration_errors: list[BaseException] = []
    started_at = time.monotonic()
    load_process = popen(command, cwd=language.example, env=load_env)
    recovery_thread: threading.Thread | None = None
    if orchestrate and scenario.kafka_enabled:
        recovery_thread = threading.Thread(
            target=orchestrate_kafka_recovery,
            args=(language, load_env, _parse_duration_seconds(duration), started_at,
                  timeline, orchestration_errors),
            daemon=False,
        )
        recovery_thread.start()
    while load_process.poll() is None:
        if runtime_metrics_url is not None:
            try:
                with urllib.request.urlopen(runtime_metrics_url, timeout=1) as response:
                    values = parse_runtime_metrics(response.read().decode("utf-8"))
                    if values:
                        samples.append(
                            {
                                "elapsed_seconds": time.monotonic() - started_at,
                                "values": values,
                            }
                        )
            except (OSError, ValueError, urllib.error.URLError):
                # A scrape may race service startup/shutdown. The profiling run is
                # still valid as long as the load generator and service succeed.
                pass
        time.sleep(0.1)
    if load_process.wait() != 0:
        raise RuntimeError(f"{language.name} load generator failed")
    if recovery_thread is not None:
        recovery_thread.join(timeout=max(30, _parse_duration_seconds(duration) * 2))
        if recovery_thread.is_alive():
            raise RuntimeError("Kafka recovery orchestration did not finish")
        timeline_path = result_path.with_suffix(".orchestration.json")
        timeline_path.write_text(json.dumps(timeline, indent=2) + "\n")
        if orchestration_errors:
            raise RuntimeError(
                f"Kafka recovery orchestration failed: {orchestration_errors[0]}"
            )
        validate_recovery_timeline(timeline, duration)
    validate_load_result(result_path, language, duration, load_env)
    if metrics_path is None:
        return
    metrics_path.write_text(json.dumps(samples, indent=2) + "\n")
    if not samples:
        raise RuntimeError(
            f"{language.name} produced no runtime metric samples from "
            f"{runtime_metrics_url}"
        )
    required_metrics = {
        "runtime_active_work",
        "runtime_event_loop_lag_seconds",
        "runtime_worker_utilization",
    }
    if env.get("CPPBOOSTSERVICELIB_COROUTINE_DIAGNOSTICS") == "ON":
        required_metrics.update(
            {
                "runtime_handler_queued",
                "runtime_handler_running",
                "runtime_handler_suspended",
            }
        )
    incomplete = [
        sample
        for sample in samples
        if not required_metrics.issubset(sample["values"])
    ]
    if incomplete:
        raise RuntimeError(
            f"{language.name} runtime metric samples are missing required "
            f"instruments: {sorted(required_metrics)}"
        )


def wait_for_redpanda(language: Language, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 60
    last_output = ""
    while time.monotonic() < deadline:
        result = run(
            compose_command(
                language, "exec", "-T", "redpanda", "rpk", "cluster", "health",
                "--exit-when-healthy",
            ),
            cwd=language.example,
            env=env,
            capture=True,
            check=False,
        )
        last_output = result.stdout + result.stderr
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Redpanda did not become healthy: {last_output}")


def validate_recovery_timeline(
    timeline: list[dict[str, Any]], duration: str
) -> None:
    if [item.get("event") for item in timeline] != [
        "redpanda_stopped", "redpanda_healthy"
    ]:
        raise RuntimeError(f"Kafka recovery timeline is incomplete: {timeline}")
    if float(timeline[-1]["elapsed_seconds"]) > _parse_duration_seconds(duration):
        raise RuntimeError(
            "Redpanda became healthy only after the measured load window"
        )


def orchestrate_kafka_recovery(
    language: Language,
    env: dict[str, str],
    duration_seconds: int,
    started_at: float,
    timeline: list[dict[str, Any]],
    errors: list[BaseException],
) -> None:
    def wait_until(seconds: float) -> None:
        remaining = started_at + seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    try:
        wait_until(max(0.5, duration_seconds / 3))
        run(
            compose_command(language, "stop", "redpanda"),
            cwd=language.example,
            env=env,
        )
        timeline.append(
            {"event": "redpanda_stopped", "elapsed_seconds": time.monotonic() - started_at}
        )
        wait_until(max(1.0, duration_seconds * 2 / 3))
        run(
            compose_command(language, "start", "redpanda"),
            cwd=language.example,
            env=env,
        )
        wait_for_redpanda(language, env)
        timeline.append(
            {"event": "redpanda_healthy", "elapsed_seconds": time.monotonic() - started_at}
        )
    except BaseException as error:  # surfaced to the owning profiling thread
        errors.append(error)


def parse_runtime_metrics(payload: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in payload.splitlines():
        if not line.startswith("runtime_"):
            continue
        raw_name, separator, value = line.rpartition(" ")
        if not separator:
            continue
        name = raw_name.split("{", 1)[0]
        values[name] = float(value)
    return values


def validate_load_result(
    path: Path,
    language: Language,
    duration: str,
    expected: dict[str, str] | None = None,
) -> None:
    if not path.is_file():
        raise RuntimeError(f"{language.name} load generator created no result {path}")
    result = json.loads(path.read_text())
    expected_scenario = (
        expected.get("PROFILING_SCENARIO", "process_order_out_of_stock")
        if expected is not None
        else "process_order_out_of_stock"
    )
    if result.get("scenario") != expected_scenario:
        raise RuntimeError(f"{language.name} load result has an unexpected scenario")
    if result.get("duration") != duration:
        raise RuntimeError(f"{language.name} load result has an unexpected duration")
    if int(result.get("request_count", 0)) <= 0:
        raise RuntimeError(f"{language.name} profiling load completed no requests")
    if int(result.get("interrupted_iterations", 0)) != 0:
        raise RuntimeError(
            f"{language.name} profiling load interrupted iterations"
        )
    if float(result.get("error_rate", 1)) != 0:
        raise RuntimeError(f"{language.name} profiling load contains request errors")
    expected_outcome = (
        expected.get("PROFILING_EXPECTED_OUTCOME", "success")
        if expected is not None
        else "success"
    )
    drops = int(result.get("dropped_iterations", 0))
    transport_error_rate = float(result.get("transport_error_rate", 0))
    if expected_outcome == "overload":
        if drops <= 0:
            raise RuntimeError(
                f"{language.name} overload profile did not reach saturation"
            )
    elif drops != 0:
        raise RuntimeError(f"{language.name} profiling load dropped iterations")
    if expected_outcome == "transport-timeout":
        if transport_error_rate <= 0:
            raise RuntimeError(
                f"{language.name} cancellation profile observed no client timeouts"
            )
    elif transport_error_rate != 0:
        raise RuntimeError(
            f"{language.name} profiling load contains transport errors"
        )
    if expected is None:
        return
    metadata = {
        "build_type": "Release",
        "vus": int(expected["PROFILING_VUS"]),
        "service_cores": int(expected["PROFILING_SERVICE_CORES"]),
        "loadgen_cores": int(expected["PROFILING_LOADGEN_CORES"]),
    }
    mismatched = {
        key: {"actual": result.get(key), "expected": value}
        for key, value in metadata.items()
        if result.get(key) != value
    }
    if mismatched:
        raise RuntimeError(
            f"{language.name} profiling load metadata differs: {mismatched}"
        )


def profile_target(
    language: Language,
    args: argparse.Namespace,
    env: dict[str, str],
    service: str,
    profiler_service: str,
    process_pattern: str,
) -> list[Path]:
    output_name = scenario_artifact_name(
        args, f"{language.name}.{service}.flamegraph.svg"
    )
    output_path = ARTIFACTS / output_name
    output_path.unlink(missing_ok=True)
    ready_name = scenario_artifact_name(
        args, f".{language.name}.{service}.cpu.ready"
    )
    ready_path = ARTIFACTS / ready_name
    ready_path.unlink(missing_ok=True)
    stop_name = scenario_artifact_name(
        args, f".{language.name}.{service}.cpu.stop"
    )
    stop_path = ARTIFACTS / stop_name
    stop_path.unlink(missing_ok=True)

    duration_seconds = _parse_duration_seconds(args.duration)
    scenario = SCENARIOS[getattr(args, "scenario", "normal")]
    capture_seconds = duration_seconds + scenario.graceful_stop_seconds
    profiler_process = popen(
        compose_command(
            language,
            "--profile",
            "profiling",
            "run",
            "--rm",
            "--no-deps",
            profiler_service,
            language.tool,
            (
                f"http://{service}:9229"
                if language.tool == "node-cpu"
                else process_pattern
            ),
            str(capture_seconds),
            f"/results/{output_name}",
            f"/results/{ready_name}",
            f"/results/{stop_name}",
        ),
        cwd=language.example,
        env=env,
    )
    wait_for_profiler_ready(ready_path, profiler_process, language, service, "CPU")
    try:
        load(
            language,
            env,
            duration=args.duration,
            result_name=scenario_artifact_name(
                args, f"{language.name}.{service}.profiling-load.json"
            ),
            runtime_metrics_url=(
                f"http://localhost:{ {'orderservice': 9091, 'inventoryservice': 9092, 'analyticsservice': 9093}[service] }/metrics"
                if language.name in {"cppboost", "typescript"}
                and "SERVICELIB_NOOP_METRICS" not in env
                else None
            ),
            runtime_metrics_name=(
                scenario_artifact_name(
                    args, f"{language.name}.{service}.runtime-metrics.json"
                )
                if language.name in {"cppboost", "typescript"}
                and "SERVICELIB_NOOP_METRICS" not in env
                else None
            ),
            orchestrate=True,
        )
    finally:
        if language.tool == "node-cpu":
            stop_path.write_text("stop\n")
    # py-spy may need extra time to drain delayed ptrace samples after the
    # measured load has finished.  A fixed 120-second timeout could therefore
    # reject a profile whose artifacts were already being finalized.
    profiler_exit_timeout = (
        # profile.sh profiles for the load duration plus its five-second
        # startup margin and allows py-spy up to 12x that window to drain.
        # Keep the parent timeout above that bounded child timeout.
        max(300, duration_seconds * 16)
        if language.tool == "pyspy"
        else 120
    )
    profiler_return_code = profiler_process.wait(timeout=profiler_exit_timeout)
    if profiler_return_code != 0:
        raise RuntimeError(
            f"{language.name} {service} profiler exited with code {profiler_return_code}"
        )
    if not output_path.exists():
        raise RuntimeError(f"{language.name} {service} profiler did not create {output_path}")
    artifacts = [
        output_path,
        Path(f"{output_path}.folded.txt"),
        Path(f"{output_path}.top.txt"),
    ]
    load_artifact = ARTIFACTS / scenario_artifact_name(
        args, f"{language.name}.{service}.profiling-load.json"
    )
    artifacts.append(load_artifact)
    if SCENARIOS[getattr(args, "scenario", "normal")].kafka_enabled:
        artifacts.append(load_artifact.with_suffix(".orchestration.json"))
    if language.tool == "node-cpu":
        raw_profile = Path(f"{output_path}.cpuprofile")
        if not raw_profile.is_file():
            raise RuntimeError(
                f"{language.name} {service} profiler did not create {raw_profile}"
            )
        runtime_profile = Path(f"{output_path}.runtime.json")
        artifacts.extend([raw_profile, runtime_profile])
    missing = [
        artifact
        for artifact in artifacts
        if not artifact.is_file() or artifact.stat().st_size == 0
    ]
    if missing:
        raise RuntimeError(
            f"{language.name} {service} profiler created no artifacts: {missing}"
        )
    if language.tool == "node-cpu":
        summary = write_node_profile_summary(
            language,
            args,
            service,
            output_path,
            runtime_profile,
            ARTIFACTS / scenario_artifact_name(
                args, f"{language.name}.{service}.profiling-load.json"
            ),
            raw_profile,
            mode="cpu",
        )
        artifacts.append(summary)
    return artifacts


def wait_for_profiler_ready(
    ready_path: Path,
    process: subprocess.Popen[str],
    language: Language,
    service: str,
    profiler: str,
) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready_path.is_file() and ready_path.read_text().strip():
            return
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"{language.name} {service} {profiler} profiler exited "
                f"before ready with code {return_code}"
            )
        time.sleep(0.05)
    raise RuntimeError(f"{language.name} {service} {profiler} profiler was not ready")


def profile_scheduler_target(
    language: Language,
    args: argparse.Namespace,
    env: dict[str, str],
    service: str,
    profiler_service: str,
    process_pattern: str,
) -> Path:
    output_name = f"{language.name}.{service}.scheduler.json"
    output_path = ARTIFACTS / output_name
    output_path.unlink(missing_ok=True)
    ready_name = f".{language.name}.{service}.scheduler.ready"
    ready_path = ARTIFACTS / ready_name
    ready_path.unlink(missing_ok=True)
    profiler_process = popen(
        compose_command(
            language,
            "--profile", "profiling", "run", "--rm", "--no-deps",
            profiler_service,
            "scheduler", process_pattern,
            str(_parse_duration_seconds(args.duration)),
            f"/results/{output_name}",
            f"/results/{ready_name}",
        ),
        cwd=language.example,
        env=env,
    )
    wait_for_profiler_ready(
        ready_path, profiler_process, language, service, "scheduler"
    )
    load(
        language,
        env,
        duration=args.duration,
        result_name=f"{language.name}.{service}.scheduler-load.json",
    )
    return_code = profiler_process.wait(timeout=120)
    if return_code != 0:
        raise RuntimeError(
            f"{language.name} {service} scheduler profiler exited with {return_code}"
        )
    if not output_path.is_file():
        raise RuntimeError(
            f"{language.name} {service} scheduler profiler created no artifact"
        )
    load_path = ARTIFACTS / f"{language.name}.{service}.scheduler-load.json"
    profile = json.loads(output_path.read_text())
    load_result = json.loads(load_path.read_text())
    profile.update(
        {
            "language": language.name,
            "service": service,
            "scenario": "process_order_out_of_stock",
            "build_type": "Release",
            "duration": args.duration,
            "vus": args.vus,
            "service_cores": args.cores,
            "loadgen_cores": args.loadgen_cores,
            "request_count": load_result["request_count"],
            "load_artifact": str(load_path),
        }
    )
    output_path.write_text(json.dumps(profile, indent=2) + "\n")
    return output_path


def profile_offcpu_target(
    language: Language,
    args: argparse.Namespace,
    env: dict[str, str],
    service: str,
    profiler_service: str,
    process_pattern: str,
) -> Path:
    output_name = f"{language.name}.{service}.offcpu.flamegraph.svg"
    output_path = ARTIFACTS / output_name
    output_path.unlink(missing_ok=True)
    ready_name = f".{language.name}.{service}.offcpu.ready"
    ready_path = ARTIFACTS / ready_name
    ready_path.unlink(missing_ok=True)
    profiler_process = popen(
        compose_command(
            language,
            "--profile", "profiling", "run", "--rm", "--no-deps",
            profiler_service,
            "offcpu", process_pattern,
            str(_parse_duration_seconds(args.duration)),
            f"/results/{output_name}",
            f"/results/{ready_name}",
        ),
        cwd=language.example,
        env=env,
    )
    wait_for_profiler_ready(
        ready_path, profiler_process, language, service, "off-CPU"
    )
    load(
        language,
        env,
        duration=args.duration,
        result_name=f"{language.name}.{service}.offcpu-load.json",
    )
    return_code = profiler_process.wait(timeout=120)
    if return_code != 0:
        raise RuntimeError(
            f"{language.name} {service} off-CPU profiler exited with {return_code}"
        )
    if not output_path.is_file():
        raise RuntimeError(
            f"{language.name} {service} off-CPU profiler created no artifact"
        )
    return output_path


def profile_language(language: Language, args: argparse.Namespace) -> list[Path]:
    env = environment(args, language)
    scenario = SCENARIOS[getattr(args, "scenario", "normal")]
    inventory_paused = False

    try:
        if scenario.kafka_enabled:
            run(
                compose_command(language, "up", "--detach", "redpanda"),
                cwd=language.example,
                env=env,
            )
            wait_for_redpanda(language, env)
            run(
                compose_command(
                    language, "up", "--detach", "--no-deps", "analyticsservice"
                ),
                cwd=language.example,
                env=env,
            )
            wait_for_service(
                language,
                "analyticsservice",
                "http://localhost:9093/status/data",
                env,
            )
        run(
            compose_command(
                language, "up", "--detach", "--no-deps", "inventoryservice"
            ),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language,
            "inventoryservice",
            "http://localhost:9092/status/data",
            env,
        )
        run(
            compose_command(language, "up", "--detach", "--no-deps", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language,
            "orderservice",
            "http://localhost:9091/status/data",
            env,
        )

        if args.warmup != "0" and args.warmup != "0s":
            load(
                language,
                env,
                duration=args.warmup,
                result_name=scenario_artifact_name(
                    args, f"{language.name}.warmup.json"
                ),
                scenario_override="normal",
            )

        if scenario.pause_inventory:
            run(
                compose_command(language, "pause", "inventoryservice"),
                cwd=language.example,
                env=env,
            )
            inventory_paused = True

        patterns = {
            "orderservice": language.order_process_pattern,
            "inventoryservice": language.inventory_process_pattern,
            "analyticsservice": language.analytics_process_pattern,
        }
        targets = TARGETS
        if scenario.pause_inventory:
            targets = (("orderservice", "profiler"),)
        elif scenario.kafka_enabled:
            targets = (
                *TARGETS,
                ("analyticsservice", "profiler-analytics"),
            )
        outputs: list[Path] = []
        if "cpu" in args.profile_kind:
            for service, profiler_service in targets:
                process_pattern = patterns[service]
                if process_pattern is None:
                    raise RuntimeError(
                        f"{language.name} has no {service} process pattern"
                    )
                print(f"--- {language.name}: CPU profiling {service} ---", flush=True)
                outputs.extend(
                    profile_target(
                        language, args, env, service, profiler_service, process_pattern
                    )
                )
        if "scheduler" in args.profile_kind:
            for service, profiler_service in TARGETS:
                print(
                    f"--- {language.name}: scheduler profiling {service} ---",
                    flush=True,
                )
                outputs.append(
                    profile_scheduler_target(
                        language,
                        args,
                        env,
                        service,
                        profiler_service,
                        patterns[service],
                    )
                )
        if "offcpu" in args.profile_kind:
            for service, profiler_service in TARGETS:
                print(
                    f"--- {language.name}: off-CPU profiling {service} ---",
                    flush=True,
                )
                outputs.append(
                    profile_offcpu_target(
                        language, args, env, service, profiler_service,
                        patterns[service],
                    )
                )
        return outputs
    finally:
        if inventory_paused:
            run(
                compose_command(language, "unpause", "inventoryservice"),
                cwd=language.example,
                env=env,
                check=False,
            )
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=env,
            check=False,
        )


ALLOCATION_RECORD = struct.Struct("<8sIIQ12Q")
ALLOCATION_COUNTERS = (
    "malloc_calls",
    "calloc_calls",
    "realloc_calls",
    "memalign_calls",
    "free_calls",
    "allocation_failures",
    "malloc_bytes",
    "calloc_bytes",
    "realloc_bytes",
    "memalign_bytes",
    "freed_bytes",
    "peak_live_bytes",
)


def signal_services(language: Language, env: dict[str, str], signal: str) -> None:
    run(
        compose_command(
            language, "kill", "--signal", signal,
            "inventoryservice", "orderservice",
        ),
        cwd=language.example,
        env=env,
    )


def parse_allocation_snapshot(
    binary_path: Path,
    output_path: Path,
    language: Language,
    service: str,
    args: argparse.Namespace,
    load_path: Path,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if binary_path.is_file() and binary_path.stat().st_size >= ALLOCATION_RECORD.size:
            break
        time.sleep(0.05)
    if not binary_path.is_file():
        raise RuntimeError(f"missing allocation snapshot {binary_path}")
    data = binary_path.read_bytes()
    if len(data) < ALLOCATION_RECORD.size:
        raise RuntimeError(f"truncated allocation snapshot {binary_path}")
    magic, version, count, pid, *values = ALLOCATION_RECORD.unpack_from(data)
    if magic != b"SLALLOC\0" or version != 1 or count != len(ALLOCATION_COUNTERS):
        raise RuntimeError(f"invalid allocation snapshot header in {binary_path}")
    counters = dict(zip(ALLOCATION_COUNTERS, values, strict=True))
    allocated_bytes = sum(
        counters[name]
        for name in ("malloc_bytes", "calloc_bytes", "realloc_bytes", "memalign_bytes")
    )
    allocation_calls = sum(
        counters[name]
        for name in ("malloc_calls", "calloc_calls", "realloc_calls", "memalign_calls")
    )
    load_result = json.loads(load_path.read_text())
    requests = int(load_result["request_count"])
    artifact = {
        "schema_version": 1,
        "profiler": "allocator-neutral-ld-preload-counters",
        "language": language.name,
        "service": service,
        "scenario": SCENARIOS[getattr(args, "scenario", "normal")].name,
        "build_type": "Release",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "pid": pid,
        "request_count": requests,
        "allocation_calls": allocation_calls,
        "allocated_bytes": allocated_bytes,
        "allocations_per_request": allocation_calls / requests,
        "allocated_bytes_per_request": allocated_bytes / requests,
        "counters": counters,
        "load_artifact": str(load_path),
    }
    output_path.write_text(json.dumps(artifact, indent=2) + "\n")


def profile_allocations(language: Language, args: argparse.Namespace) -> list[Path]:
    env = environment(args, language)
    env.update(
        {
            "PROFILING_LD_PRELOAD": "/profiling-tools/liballocation_profile.so",
            "PROFILING_ALLOCATION_STACK_SAMPLE_EVERY":
                str(args.allocation_stack_sample_every),
            "PROFILING_ALLOCATION_INVENTORY_FILE":
                f".{language.name}.inventoryservice.alloc.bin",
            "PROFILING_ALLOCATION_ORDER_FILE":
                f".{language.name}.orderservice.alloc.bin",
            "PROFILING_ALLOCATION_STACK_INVENTORY_FILE":
                f".{language.name}.inventoryservice.alloc-stacks.bin",
            "PROFILING_ALLOCATION_STACK_ORDER_FILE":
                f".{language.name}.orderservice.alloc-stacks.bin",
        }
    )
    binaries = {
        service: ARTIFACTS / env[variable]
        for service, variable in (
            ("inventoryservice", "PROFILING_ALLOCATION_INVENTORY_FILE"),
            ("orderservice", "PROFILING_ALLOCATION_ORDER_FILE"),
        )
    }
    stack_binaries = {
        service: ARTIFACTS / env[variable]
        for service, variable in (
            ("inventoryservice", "PROFILING_ALLOCATION_STACK_INVENTORY_FILE"),
            ("orderservice", "PROFILING_ALLOCATION_STACK_ORDER_FILE"),
        )
    }
    for path in (*binaries.values(), *stack_binaries.values()):
        path.unlink(missing_ok=True)
    try:
        run(
            compose_command(language, "up", "--detach", "--no-deps", "inventoryservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language, "inventoryservice", "http://localhost:9092/status/data", env
        )
        run(
            compose_command(language, "up", "--detach", "--no-deps", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language, "orderservice", "http://localhost:9091/status/data", env
        )
        if args.warmup not in {"0", "0s"}:
            load(
                language, env, duration=args.warmup,
                result_name=f"{language.name}.allocation-warmup.json",
            )
        signal_services(language, env, "SIGUSR1")
        time.sleep(0.1)
        load_name = f"{language.name}.allocation.profiling-load.json"
        load(language, env, duration=args.duration, result_name=load_name)
        signal_services(language, env, "SIGUSR2")
        time.sleep(0.1)
        load_path = ARTIFACTS / load_name
        outputs: list[Path] = []
        for service, binary_path in binaries.items():
            output = ARTIFACTS / f"{language.name}.{service}.allocations.json"
            parse_allocation_snapshot(
                binary_path, output, language, service, args, load_path
            )
            outputs.append(output)
        patterns = {
            "orderservice": language.order_process_pattern,
            "inventoryservice": language.inventory_process_pattern,
        }
        profiler_services = dict(TARGETS)
        for service, stack_binary in stack_binaries.items():
            output_name = (
                f"{language.name}.{service}.allocation-stacks.flamegraph.svg"
            )
            output = ARTIFACTS / output_name
            derived = [
                output,
                Path(f"{output}.folded.txt"),
                Path(f"{output}.bytes.folded.txt"),
                Path(f"{output}.top.txt"),
                Path(f"{output}.summary.json"),
                Path(f"{output}.maps.txt"),
            ]
            for path in derived:
                path.unlink(missing_ok=True)
            run(
                compose_command(
                    language,
                    "--profile", "profiling", "run", "--rm", "--no-deps",
                    profiler_services[service],
                    "allocation-stacks", patterns[service],
                    f"/results/{stack_binary.name}", f"/results/{output_name}",
                ),
                cwd=language.example,
                env=env,
            )
            missing = [
                path
                for path in derived
                if not path.is_file() or path.stat().st_size == 0
            ]
            if missing:
                raise RuntimeError(
                    f"{language.name} {service} allocation stack profiler "
                    f"created no artifacts: {missing}"
                )
            summary_path = Path(f"{output}.summary.json")
            summary = json.loads(summary_path.read_text())
            summary.update(
                {
                    "language": language.name,
                    "service": service,
                    "scenario": "process_order_out_of_stock",
                    "build_type": "Release",
                    "duration": args.duration,
                    "vus": args.vus,
                    "service_cores": args.cores,
                    "loadgen_cores": args.loadgen_cores,
                    "request_count": json.loads(load_path.read_text())[
                        "request_count"
                    ],
                    "load_artifact": str(load_path),
                }
            )
            summary_path.write_text(json.dumps(summary, indent=2) + "\n")
            outputs.extend(derived)
        return outputs
    finally:
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=env,
            check=False,
        )


def profile_node_allocations(
    language: Language, args: argparse.Namespace
) -> list[Path]:
    """Capture V8 sampling heap profiles without interposing Node's allocator."""
    env = environment(args, language)
    try:
        run(
            compose_command(language, "up", "--detach", "--no-deps", "inventoryservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language, "inventoryservice", "http://localhost:9092/status/data", env
        )
        run(
            compose_command(language, "up", "--detach", "--no-deps", "orderservice"),
            cwd=language.example,
            env=env,
        )
        wait_for_service(
            language, "orderservice", "http://localhost:9091/status/data", env
        )
        if args.warmup not in {"0", "0s"}:
            load(
                language,
                env,
                duration=args.warmup,
                result_name=f"{language.name}.allocation-warmup.json",
            )

        outputs: list[Path] = []
        for service, profiler_service in TARGETS:
            output_name = (
                f"{language.name}.{service}.allocation-stacks.flamegraph.svg"
            )
            output_path = ARTIFACTS / output_name
            ready_name = f".{language.name}.{service}.allocation.ready"
            ready_path = ARTIFACTS / ready_name
            derived = [
                output_path,
                Path(f"{output_path}.folded.txt"),
                Path(f"{output_path}.top.txt"),
                Path(f"{output_path}.heapprofile"),
                Path(f"{output_path}.heapsnapshot"),
                Path(f"{output_path}.runtime.json"),
            ]
            for path in (*derived, ready_path):
                path.unlink(missing_ok=True)

            profiler_process = popen(
                compose_command(
                    language,
                    "--profile",
                    "profiling",
                    "run",
                    "--rm",
                    "--no-deps",
                    profiler_service,
                    "node-heap",
                    f"http://{service}:9229",
                    str(_parse_duration_seconds(args.duration)),
                    f"/results/{output_name}",
                    f"/results/{ready_name}",
                ),
                cwd=language.example,
                env=env,
            )
            wait_for_profiler_ready(
                ready_path, profiler_process, language, service, "V8 heap"
            )
            load(
                language,
                env,
                duration=args.duration,
                result_name=f"{language.name}.{service}.allocation-load.json",
            )
            return_code = profiler_process.wait(timeout=120)
            if return_code != 0:
                raise RuntimeError(
                    f"{language.name} {service} V8 heap profiler exited with "
                    f"{return_code}"
                )
            missing = [path for path in derived if not path.is_file()]
            if missing:
                raise RuntimeError(
                    f"{language.name} {service} V8 heap profiler created no "
                    f"artifacts: {missing}"
                )
            summary = write_node_profile_summary(
                language,
                args,
                service,
                output_path,
                Path(f"{output_path}.runtime.json"),
                ARTIFACTS / f"{language.name}.{service}.allocation-load.json",
                Path(f"{output_path}.heapprofile"),
                mode="heap",
                heap_snapshot_path=Path(f"{output_path}.heapsnapshot"),
            )
            outputs.extend([*derived, summary])
        return outputs
    finally:
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=env,
            check=False,
        )


def _parse_duration_seconds(duration: str) -> int:
    duration = duration.strip()
    if duration.endswith("ms"):
        return max(1, int(duration[:-2]) // 1000)
    if duration.endswith("s"):
        return int(duration[:-1])
    if duration.endswith("m"):
        return int(duration[:-1]) * 60
    return int(duration)


def write_node_profile_summary(
    language: Language,
    args: argparse.Namespace,
    service: str,
    output_path: Path,
    runtime_path: Path,
    load_path: Path,
    raw_profile_path: Path,
    *,
    mode: str,
    heap_snapshot_path: Path | None = None,
) -> Path:
    runtime = json.loads(runtime_path.read_text())
    load_result = json.loads(load_path.read_text())
    samples = runtime.get("samples", [])
    if not isinstance(samples, list) or not samples:
        raise RuntimeError(f"{runtime_path} contains no runtime samples")

    gc_events = [
        event
        for sample in samples
        for event in sample.get("gc", [])
        if isinstance(event, dict)
    ]
    memory_samples = [
        sample.get("memory", {})
        for sample in samples
        if isinstance(sample.get("memory"), dict)
    ]
    request_count = int(load_result["request_count"])
    sampled_allocation_bytes = 0
    if mode == "heap":
        raw_profile = json.loads(raw_profile_path.read_text())
        sampled_allocation_bytes = sum(
            int(sample.get("size", 0))
            for sample in raw_profile.get("samples", [])
            if isinstance(sample, dict)
        )

    metadata = {
        "language": language.name,
        "service": service,
        "scenario": SCENARIOS[getattr(args, "scenario", "normal")].name,
        "profile_mode": mode,
        "build_type": "Release",
        "duration": args.duration,
        "vus": args.vus,
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "request_count": request_count,
        "load_artifact": str(load_path),
        "raw_profile_artifact": str(raw_profile_path),
        "runtime_artifact": str(runtime_path),
    }
    runtime.update(metadata)
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    summary: dict[str, Any] = {
        **metadata,
        "requests_per_second": float(load_result["requests_per_second"]),
        "error_rate": float(load_result["error_rate"]),
        "dropped_iterations": int(load_result["dropped_iterations"]),
        "latency_ms": load_result["latency_ms"],
        "runtime": {
            "sample_count": len(samples),
            "event_loop_utilization_avg": average(
                samples, "eventLoopUtilization"
            ),
            "event_loop_utilization_max": maximum(
                samples, "eventLoopUtilization"
            ),
            "event_loop_lag_max_seconds": maximum(
                samples, "eventLoopLagMaxSeconds"
            ),
            "cpu_user_seconds": total(samples, "cpuUserSeconds"),
            "cpu_system_seconds": total(samples, "cpuSystemSeconds"),
            "event_loop_active_seconds": total(samples, "eventLoopActiveSeconds"),
            "event_loop_idle_seconds": total(samples, "eventLoopIdleSeconds"),
            "active_resources_max": maximum(samples, "activeResources"),
            "gc_collections": len(gc_events),
            "gc_pause_seconds": sum(
                float(event.get("durationSeconds", 0)) for event in gc_events
            ),
            "heap_used_bytes_max": max(
                (int(memory.get("heapUsed", 0)) for memory in memory_samples),
                default=0,
            ),
            "rss_bytes_max": max(
                (int(memory.get("rss", 0)) for memory in memory_samples),
                default=0,
            ),
        },
    }
    if mode == "heap":
        summary["sampled_allocation_bytes"] = sampled_allocation_bytes
        summary["sampled_allocation_bytes_per_request"] = (
            sampled_allocation_bytes / request_count
        )
        if heap_snapshot_path is None:
            raise RuntimeError("Node heap profile has no retained-heap snapshot")
        summary["heap_snapshot"] = summarize_heap_snapshot(heap_snapshot_path)
    canonical_metrics = ARTIFACTS / scenario_artifact_name(
        args, f"{language.name}.{service}.runtime-metrics.json"
    )
    if canonical_metrics.is_file() and language.name == "typescript" and mode == "cpu":
        summary["canonical_runtime_metrics_artifact"] = str(canonical_metrics)

    summary_path = Path(f"{output_path}.summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary_path


def summarize_heap_snapshot(path: Path) -> dict[str, int | str]:
    """Validate a V8 snapshot and expose stable retained-heap inventory totals."""
    with path.open() as stream:
        snapshot = json.load(stream)
    metadata = snapshot.get("snapshot", {}).get("meta", {})
    fields = metadata.get("node_fields", [])
    nodes = snapshot.get("nodes", [])
    if not isinstance(fields, list) or not fields:
        raise RuntimeError(f"{path} has no V8 heap snapshot node schema")
    if not isinstance(nodes, list) or len(nodes) % len(fields) != 0:
        raise RuntimeError(f"{path} has malformed V8 heap snapshot nodes")
    try:
        self_size_index = fields.index("self_size")
    except ValueError as error:
        raise RuntimeError(f"{path} has no V8 self_size field") from error
    self_size_bytes = sum(
        int(nodes[index + self_size_index])
        for index in range(0, len(nodes), len(fields))
    )
    return {
        "artifact": str(path),
        "bytes": path.stat().st_size,
        "node_count": len(nodes) // len(fields),
        "self_size_bytes": self_size_bytes,
    }


def average(samples: list[dict[str, Any]], name: str) -> float:
    return total(samples, name) / len(samples)


def total(samples: list[dict[str, Any]], name: str) -> float:
    return sum(float(sample.get(name, 0)) for sample in samples)


def maximum(samples: list[dict[str, Any]], name: str) -> float:
    return max((float(sample.get(name, 0)) for sample in samples), default=0)


def write_typescript_comparison(outputs: list[Path]) -> list[Path]:
    summaries = [
        json.loads(path.read_text())
        for path in outputs
        if path.name.endswith(".summary.json")
        and path.name.startswith(("typescript.", "typescript-native."))
    ]
    written: list[Path] = []
    for mode in ("cpu", "heap"):
        selected = [item for item in summaries if item.get("profile_mode") == mode]
        languages = {str(item.get("language")) for item in selected}
        if languages != {"typescript", "typescript-native"}:
            continue
        scenario = str(selected[0]["scenario"])
        if any(str(item["scenario"]) != scenario for item in selected):
            raise RuntimeError("TypeScript comparison contains mixed scenarios")
        payload = {
            "profile_mode": mode,
            "scenario": scenario,
            "results": sorted(
                selected,
                key=lambda item: (str(item["service"]), str(item["language"])),
            ),
        }
        scenario_suffix = ""
        if scenario != SCENARIOS["normal"].name:
            scenario_key = next(
                item.key for item in SCENARIOS.values() if item.name == scenario
            )
            scenario_suffix = f".{scenario_key}"
        json_path = ARTIFACTS / (
            f"typescript.framework-native.{mode}{scenario_suffix}.json"
        )
        markdown_path = ARTIFACTS / (
            f"typescript.framework-native.{mode}{scenario_suffix}.md"
        )
        json_path.write_text(json.dumps(payload, indent=2) + "\n")
        markdown_path.write_text(render_typescript_comparison(payload))
        written.extend([json_path, markdown_path])
    return written


def render_typescript_comparison(payload: dict[str, Any]) -> str:
    lines = [
        f"# TypeScript framework/native {payload['profile_mode']} profile",
        "",
        "| Service | Variant | RPS | p50 ms | p95 ms | p99 ms | Max ms | CPU s | ELU avg | Lag max ms | Event-loop idle s | GC count | GC pause ms | Sampled alloc B/request | Post-GC heap self-size MiB |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in payload["results"]:
        runtime = result["runtime"]
        allocation = result.get("sampled_allocation_bytes_per_request")
        heap_snapshot = result.get("heap_snapshot", {})
        heap_self_size = heap_snapshot.get("self_size_bytes")
        lines.append(
            "| {service} | {language} | {rps:.2f} | {p50:.2f} | {p95:.2f} | "
            "{p99:.2f} | {maximum:.2f} | {cpu:.3f} | {elu:.3f} | {lag:.2f} | "
            "{idle:.3f} | {gc:d} | {gc_pause:.2f} | {allocation} | "
            "{heap_self_size} |".format(
                service=result["service"],
                language=result["language"],
                rps=result["requests_per_second"],
                p50=result["latency_ms"]["p50"],
                p95=result["latency_ms"]["p95"],
                p99=result["latency_ms"]["p99"],
                maximum=result["latency_ms"]["max"],
                cpu=(
                    runtime["cpu_user_seconds"] + runtime["cpu_system_seconds"]
                ),
                elu=runtime["event_loop_utilization_avg"],
                lag=runtime["event_loop_lag_max_seconds"] * 1_000,
                idle=runtime["event_loop_idle_seconds"],
                gc=runtime["gc_collections"],
                gc_pause=runtime["gc_pause_seconds"] * 1_000,
                allocation=(
                    f"{allocation:.2f}" if allocation is not None else "—"
                ),
                heap_self_size=(
                    f"{heap_self_size / (1024 * 1024):.2f}"
                    if heap_self_size is not None
                    else "—"
                ),
            )
        )
    return "\n".join(lines) + "\n"


def lower_perf_paranoid() -> None:
    """perf_event_paranoid is a global, non-namespaced host sysctl. Docker
    Desktop's Linux VM defaults to 2 (profiling other processes blocked);
    lower it once via a throwaway privileged container so `perf record -p
    <pid>` works from the profiler sidecar without requiring the whole
    profiler container to run --privileged."""
    subprocess.run(
        [
            "docker", "run", "--rm", "--privileged", "debian:bookworm-slim",
            "sh", "-c", "echo 1 > /proc/sys/kernel/perf_event_paranoid",
        ],
        check=True,
    )


def raise_max_map_count(value: int) -> None:
    """vm.max_map_count is a global, non-namespaced host sysctl -- Docker
    does not allow setting it per-container. userver mmaps a stack per
    coroutine, and this example's pipeline fans out into far more coroutines
    than VUs, so higher-concurrency profiling runs can exhaust the host's
    default limit and make every request fail with "Failed to allocate a
    coroutine (ENOMEM)". Raise it once via a throwaway --privileged
    container before profiling starts; this persists host/VM-wide across
    all subsequent containers."""
    subprocess.run(
        [
            "docker", "run", "--rm", "--privileged", "debian:bookworm-slim",
            "sh", "-c", f"echo {value} > /proc/sys/vm/max_map_count",
        ],
        check=True,
    )


def clean() -> None:
    for language in LANGUAGES:
        args = argparse.Namespace(cores=1, loadgen_cores=1, duration="1s", vus=1)
        run(
            compose_command(language, "down", "--volumes", "--remove-orphans"),
            cwd=language.example,
            env=environment(args, language),
            check=False,
        )
    import shutil
    shutil.rmtree(ARTIFACTS, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture CPU flamegraphs for equivalent ServiceLib examples "
        "under the same load used by benchmarks/."
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument("--vus", type=int, default=256)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument(
        "--graph-profile",
        choices=("function-call", "current"),
        default="function-call",
        help="generated graph profile recorded in profiling metadata",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        default="normal",
        help=(
            "profile normal success, timeout, client cancellation, overload, "
            "or Kafka broker failure/recovery (non-normal scenarios are CPU-only)"
        ),
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=100_000,
        help="requested iterations/s for the overload scenario",
    )
    parser.add_argument(
        "--max-map-count",
        type=int,
        default=0,
        help="vm.max_map_count to set host/VM-wide before profiling (0 to leave it untouched)",
    )
    parser.add_argument(
        "--prepare-host-profiling",
        action="store_true",
        help=(
            "lower the host/VM-wide perf_event_paranoid setting with a "
            "privileged container; omitted by default because this is a "
            "persistent host-wide change"
        ),
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--fetch-native",
        action="store_true",
        help="fetch or update the pinned native profiling projects and exit",
    )
    parser.add_argument(
        "--coroutine-diagnostics",
        action="store_true",
        help=(
            "enable intrusive Boost.Asio queued/running/suspended handler "
            "diagnostics for cppboost profiling only"
        ),
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--language",
        action="append",
        choices=[language.name for language in LANGUAGES],
    )
    parser.add_argument(
        "--profile-kind",
        action="append",
        choices=("cpu", "allocation", "scheduler", "offcpu"),
        help="profiler mode; repeat for a matrix (default: cpu)",
    )
    parser.add_argument(
        "--allocation-stack-sample-every",
        type=int,
        default=4096,
        help=(
            "capture one profiling-only allocation call stack per N calls "
            "during allocation profiles (default: 4096)"
        ),
    )
    args = parser.parse_args()
    if args.fetch_native:
        env = os.environ.copy()
        for language in LANGUAGES:
            if language.repository is not None:
                ensure_example(language, env)
        return 0
    try:
        acquire_tooling_lock()
    except RuntimeError as error:
        parser.error(str(error))
    if not args.profile_kind:
        args.profile_kind = ["cpu"]
    args.profile_kind = tuple(dict.fromkeys(args.profile_kind))

    if args.clean:
        clean()
        return 0
    if args.max_map_count < 0:
        parser.error("--max-map-count must not be negative")
    if args.allocation_stack_sample_every <= 0:
        parser.error("--allocation-stack-sample-every must be positive")
    if args.rate <= 0:
        parser.error("--rate must be positive")
    if args.scenario != "normal" and args.profile_kind != ("cpu",):
        parser.error("non-normal scenarios support --profile-kind cpu only")
    if args.scenario != "normal":
        unsupported = set(args.language or ()) - {
            "typescript", "typescript-native"
        }
        if unsupported:
            parser.error(
                "non-normal scenarios currently validate the TypeScript "
                f"framework/native pair only; unsupported: {sorted(unsupported)}"
            )

    selected = [
        language
        for language in LANGUAGES
        if (
            (args.language and language.name in args.language)
            or (
                not args.language
                and (
                    args.scenario == "normal"
                    or language.name in {"typescript", "typescript-native"}
                )
            )
        )
    ]
    dependency_environment = os.environ.copy()
    for language in selected:
        if language.repository is not None:
            ensure_example(language, dependency_environment)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    prepare_selected_configs(selected, args.cores)
    if args.prepare_host_profiling:
        lower_perf_paranoid()
    if args.max_map_count:
        raise_max_map_count(args.max_map_count)

    if not args.skip_build:
        build_profiler_image(os.environ.copy())
        extract_profiler_assets(os.environ.copy())
        for language in selected:
            build(language, environment(args, language))
    elif not (ARTIFACTS / "liballocation_profile.so").is_file():
        extract_profiler_assets(os.environ.copy())

    if any(language.name == "cppboost" for language in selected):
        cppboost = next(
            language for language in selected if language.name == "cppboost"
        )
        verify_cppboost_release_build(
            environment(args, cppboost),
            require_coroutine_diagnostics=args.coroutine_diagnostics,
        )

    outputs = [write_run_manifest(args, selected)]
    for language in selected:
        print(f"\n=== {language.name} ===", flush=True)
        if {"cpu", "scheduler", "offcpu"} & set(args.profile_kind):
            outputs.extend(profile_language(language, args))
        if "allocation" in args.profile_kind:
            if language.tool == "node-cpu":
                outputs.extend(profile_node_allocations(language, args))
            else:
                outputs.extend(profile_allocations(language, args))

    outputs.extend(write_typescript_comparison(outputs))

    print("\nProfiling artifacts written:", flush=True)
    for output in outputs:
        print(f"  {output}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
