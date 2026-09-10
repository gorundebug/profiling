"""Run dependency-boundary commands with same-route network retries."""

from __future__ import annotations

import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Sequence, TextIO
from urllib.parse import urlsplit, urlunsplit


TRANSIENT_NETWORK_MARKERS = (
    "context deadline exceeded",
    "could not resolve host",
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "couldn't connect to server",
    "failed to connect to",
    "failed to do request",
    "network is unreachable",
    "no route to host",
    "temporary failure in name resolution",
    "tls handshake timeout",
    "unexpected eof",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "status code: 429",
    "status code: 502",
    "status code: 503",
    "status code: 504",
    "status_code: 429",
    "status_code: 502",
    "status_code: 503",
    "status_code: 504",
)


def docker_git_context(
    environment: dict[str, str], remote_context: str,
) -> str:
    """Use the container-reachable Git mirror for a BuildKit context."""
    if not environment.get("DEPENDENCY_PROXY_DIR"):
        return remote_context
    mirror = environment.get("DEPENDENCY_GIT_MIRROR_URL")
    if not mirror:
        raise RuntimeError(
            "proxy mode requires DEPENDENCY_GIT_MIRROR_URL for remote Git contexts"
        )
    repository, separator, revision = remote_context.partition("#")
    if not separator or not revision:
        raise ValueError(f"Git context must contain a pinned revision: {remote_context}")
    parsed_repository = urlsplit(repository)
    if not parsed_repository.hostname or not parsed_repository.path:
        raise ValueError(f"Git context must use an absolute repository URL: {remote_context}")
    proxy_host = environment.get("DEPENDENCY_PROXY_HOST", "localhost")
    docker_host = environment.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    parsed_mirror = urlsplit(mirror)
    mirror_host = parsed_mirror.hostname
    if mirror_host in {proxy_host, "localhost", "127.0.0.1", "::1"}:
        netloc = docker_host
        if parsed_mirror.port is not None:
            netloc += f":{parsed_mirror.port}"
        mirror = urlunsplit(parsed_mirror._replace(netloc=netloc))
    return (
        f"{mirror.rstrip('/')}/{parsed_repository.hostname}/"
        f"{parsed_repository.path.lstrip('/')}#{revision}"
    )


def is_transient_network_failure(output: Iterable[str]) -> bool:
    return any(
        marker in line.lower()
        for line in output
        for marker in TRANSIENT_NETWORK_MARKERS
    )


def run(
    command: Sequence[str], *, cwd: Path, env: dict[str, str],
    attempts: int = 10, retry_delay_seconds: float = 2.0,
    output_stream: TextIO | None = None, echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Retry only proven transient network failures without changing route."""
    if attempts < 1:
        raise ValueError("dependency command attempts must be positive")
    rendered = " ".join(command)
    for attempt in range(1, attempts + 1):
        output: list[str] = []
        tail: deque[str] = deque(maxlen=80)
        process = subprocess.Popen(
            list(command), cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("dependency command output pipe was not created")
        for line in process.stdout:
            if echo:
                print(line, end="", flush=True)
            if output_stream is not None:
                output_stream.write(line)
                output_stream.flush()
            output.append(line)
            tail.append(line)
        return_code = process.wait()
        completed = subprocess.CompletedProcess(
            list(command), return_code, stdout="".join(output), stderr=None,
        )
        if return_code == 0:
            return completed
        if attempt == attempts or not is_transient_network_failure(tail):
            raise subprocess.CalledProcessError(
                return_code, list(command), output=completed.stdout,
            )
        delay = min(retry_delay_seconds * attempt, 15.0)
        notice = (
            "[dependency] transient network failure; retrying the same "
            f"command and route in {delay:g}s ({attempt}/{attempts}): "
            f"{rendered}\n"
        )
        if echo:
            print(notice, end="", flush=True)
        if output_stream is not None:
            output_stream.write(notice)
            output_stream.flush()
        time.sleep(delay)
    raise AssertionError("dependency retry loop terminated unexpectedly")
