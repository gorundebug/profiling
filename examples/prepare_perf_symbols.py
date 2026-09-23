#!/usr/bin/env python3
"""Decompress embedded DWARF in captured ELF copies before offline decoding."""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from collect_perf_debug import elf_info


def compressed_debug_sections(path: Path) -> list[str]:
    result = subprocess.run(
        ["readelf", "--wide", "--section-headers", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
        env={**os.environ, "LC_ALL": "C"},
    )
    sections = []
    for line in result.stdout.splitlines():
        if not line.lstrip().startswith("[") or "]" not in line:
            continue
        fields = line.split("]", 1)[1].split()
        if len(fields) < 7:
            continue
        name = fields[0]
        if name.startswith(".zdebug_") or (
            name.startswith(".debug_") and "C" in fields[6]
        ):
            sections.append(name)
    return sections


def prepare(root: Path) -> dict:
    root = root.resolve(strict=True)
    manifest = json.loads((root / ".debug-info.json").read_text())
    started = time.monotonic()
    files = []
    for library, info in sorted(manifest["libraries"].items()):
        # Detached debug companions may be referenced by .gnu_debuglink CRC.
        # Leave those files and the shared download cache byte-for-byte intact.
        if info.get("status") != "embedded":
            continue
        path = (root / library.lstrip("/")).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError(f"Symbol path escapes captured root: {library}")
        sections = compressed_debug_sections(path)
        if not sections:
            continue
        original = path.stat()
        before = elf_info(path)
        fd, name = tempfile.mkstemp(prefix=".perf-dwarf-", dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            subprocess.run(
                ["objcopy", "--decompress-debug-sections", str(path), str(temporary)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            after = elf_info(temporary)
            if not after.get("dwarf") or after.get("build_id") != before.get("build_id"):
                raise RuntimeError(f"DWARF/build ID changed while preparing {library}")
            if compressed_debug_sections(temporary):
                raise RuntimeError(f"Compressed DWARF remains in {library}")
            size = temporary.stat().st_size
            os.chmod(temporary, stat.S_IMODE(original.st_mode))
            # Replacement also avoids writing through any captured hard link.
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        files.append({
            "library": library,
            "sections": sections,
            "build_id": before.get("build_id"),
            "original_bytes": original.st_size,
            "prepared_bytes": size,
        })
    return {"files": files, "elapsed_seconds": time.monotonic() - started}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    report = prepare(args.root)
    print(json.dumps(report, indent=2))
    print(
        f"profile.sh: decompressed embedded DWARF in {len(report['files'])} "
        f"captured ELF files in {report['elapsed_seconds']:.2f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
