"""Content provenance for a local source tree, without invoking Git."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Exact directory names, not build*: e.g. build_order business code is input.
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".dependencies", ".artifacts", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "build", "target", ".nuxt", ".output",
})


def content_source_identity(path: Path) -> dict:
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    entries: dict[str, dict] = {}
    excluded: list[str] = []
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        descend = []
        for name in sorted(directories):
            item = parent / name
            relative = item.relative_to(root).as_posix()
            if name in EXCLUDED_DIRECTORIES:
                excluded.append(relative)
            elif item.is_symlink():
                entries[relative] = {"kind": "symlink", "target": os.readlink(item)}
            else:
                descend.append(name)
        directories[:] = descend
        for name in sorted(files):
            item = parent / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                entries[relative] = {"kind": "symlink", "target": os.readlink(item)}
                continue
            before = item.stat()
            digest = hashlib.sha256()
            with item.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = item.stat()
            fields = ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(getattr(before, field) != getattr(after, field) for field in fields):
                raise RuntimeError(f"source changed while hashing: {item}")
            entries[relative] = {
                "kind": "file", "sha256": digest.hexdigest(),
                "size": after.st_size, "executable": bool(after.st_mode & 0o111),
            }
    serialized = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "path": str(root), "mode": "content", "algorithm": "sha256",
        "tree_sha256": hashlib.sha256(serialized).hexdigest(),
        "entry_count": len(entries), "entries": entries,
        "excluded_directory_names": sorted(EXCLUDED_DIRECTORIES),
        "excluded_directories": sorted(excluded),
        "limitations": [
            "This is a filesystem observation, not an atomic source snapshot or Git revision.",
            "Symlink targets are recorded, not followed; external source contexts need separate identities.",
            "Build/cache directories listed here are excluded; use immutable staged inputs for comparisons.",
        ],
    }
