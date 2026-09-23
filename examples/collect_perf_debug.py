#!/usr/bin/env python3
"""Preserve matching dependency debuginfo without changing the sampled binaries."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_URLS = "https://debuginfod.ubuntu.com https://debuginfod.debian.net"


def readelf(path: Path, option: str) -> str:
    result = subprocess.run(
        ["readelf", option, str(path)], capture_output=True, text=True,
        errors="replace", timeout=30, env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise ValueError(f"Cannot read ELF {path}: {result.stderr.strip()}")
    return result.stdout


def elf_info(path: Path) -> dict:
    notes = readelf(path, "-nW")
    sections = readelf(path, "-SW")
    build_id = re.search(r"Build ID: ([0-9a-fA-F]+)", notes)
    debug_link = None
    if ".gnu_debuglink" in sections:
        match = re.search(r"\[\s*0\]\s+([^\r\n]+)", readelf(path, "--string-dump=.gnu_debuglink"))
        if match and "/" not in match[1] and match[1] not in (".", ".."):
            debug_link = match[1].strip()
    return {
        "build_id": build_id[1].lower() if build_id else None,
        "dwarf": bool(re.search(r"\.(?:zdebug_info|debug_info)\s", sections)),
        "symbol_table": bool(re.search(r"\.symtab\s", sections)),
        "debug_link": debug_link,
    }


def matching_debug(path: Path, build_id: str) -> bool:
    try:
        info = elf_info(path)
        return info["build_id"] == build_id and info["dwarf"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def fetch_debug(build_id: str, cache: Path, urls: list[str], errors: list[str]) -> Path | None:
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{build_id}.debug"
    if cached.is_file() and matching_debug(cached, build_id):
        return cached
    for server in urls:
        if not server.startswith(("https://", "http://")):
            errors.append(f"Unsupported debuginfod URL: {server}")
            continue
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            temporary = Path(directory) / "debuginfo"
            url = f"{server.rstrip('/')}/buildid/{build_id}/debuginfo"
            result = subprocess.run(
                ["curl", "--fail", "--location", "--silent", "--show-error",
                 "--proto", "=http,https", "--proto-redir", "=http,https",
                 "--connect-timeout", "10", "--max-time", "45",
                 "--max-filesize", "536870912", "--output", str(temporary), url],
                capture_output=True, text=True, timeout=50,
            )
            if result.returncode:
                errors.append(f"{server}: {result.stderr.strip()}")
                continue
            if not matching_debug(temporary, build_id):
                errors.append(f"{server}: rejected debuginfo with wrong build ID or no DWARF")
                continue
            temporary.replace(cached)
            return cached
    return None


def mapped_files(paths: list[Path]) -> list[str]:
    files = set()
    for path in paths:
        for line in path.read_text().splitlines():
            columns = line.split(None, 5)
            if len(columns) != 6 or "x" not in columns[1] or not columns[5].startswith("/"):
                continue
            filename = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), columns[5])
            if not filename.endswith(" (deleted)"):
                files.add(filename)
    return sorted(files)


def collect(root: Path, files: list[str], cache: Path, urls: list[str]) -> dict:
    root = root.resolve(strict=True)
    entries = {}
    for filename in files:
        entry = {"status": "missing", "errors": []}
        entries[filename] = entry
        try:
            binary = (root / filename.lstrip("/")).resolve(strict=True)
            if not binary.is_relative_to(root):
                raise ValueError("ELF escapes symbol root")
            info = elf_info(binary)
            entry.update(info)
            if info["dwarf"]:
                entry["status"] = "embedded"
                continue
            build_id = info["build_id"]
            if not build_id:
                entry["status"] = "no-build-id"
                continue
            destination = root / "usr/lib/debug/.build-id" / build_id[:2] / f"{build_id[2:]}.debug"
            candidates = [destination, root / f"usr/lib/debug{filename}.debug"]
            if info["debug_link"]:
                link = info["debug_link"]
                candidates += [binary.parent / link, binary.parent / ".debug" / link,
                               root / "usr/lib/debug" / filename.lstrip("/").rsplit("/", 1)[0] / link]
            companion = next((p for p in candidates if p.is_file()
                              and p.resolve().is_relative_to(root) and matching_debug(p, build_id)), None)
            source = "installed"
            if companion is None:
                companion = fetch_debug(build_id, cache, urls, entry["errors"])
                source = "cache-or-debuginfod"
            if companion is None:
                entry["status"] = "symbols-only" if info["symbol_table"] else "missing-debuginfo"
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if companion.resolve() != destination.resolve():
                shutil.copyfile(companion, destination)
            # perf also searches the debuglink relative to the relocated DSO.
            if info["debug_link"]:
                link_path = binary.parent / ".debug" / info["debug_link"]
                link_path.parent.mkdir(parents=True, exist_ok=True)
                if link_path.is_symlink() or link_path.exists():
                    link_path.unlink()
                link_path.symlink_to(os.path.relpath(destination, link_path.parent))
            entry.update(status="resolved", source=source,
                         debug_file="/" + str(destination.relative_to(root)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            entry["errors"].append(str(error))
    return {"libraries": entries, "unresolved_libraries": sum(
        item["status"] not in ("embedded", "resolved") for item in entries.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--maps", type=Path, nargs="+", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.root, mapped_files(args.maps), args.cache,
                     os.environ.get("DEBUGINFOD_URLS", DEFAULT_URLS).split())
    (args.root / ".debug-info.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["unresolved_libraries"]:
        print(f"WARNING: {report['unresolved_libraries']} ELF files lack matching DWARF; see .debug-info.json",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
