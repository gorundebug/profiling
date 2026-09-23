import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyze_perf_visibility import summarize_visibility
from collect_perf_debug import collect, fetch_debug, mapped_files, matching_debug
from perf_addr2line import resolve_arguments


class DebugInfoTests(unittest.TestCase):
    def test_wrong_id_or_no_dwarf_is_rejected(self):
        for info in ({"build_id": "bbbb", "dwarf": True}, {"build_id": "aaaa", "dwarf": False}):
            with patch("collect_perf_debug.elf_info", return_value=info):
                self.assertFalse(matching_debug(Path("debug"), "aaaa"))

    def test_embedded_dwarf_needs_no_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").touch()
            with patch("collect_perf_debug.elf_info", return_value={"dwarf": True}), \
                    patch("collect_perf_debug.fetch_debug") as fetch:
                result = collect(root, ["/app"], root / "cache", [])
            fetch.assert_not_called()
            self.assertEqual(result["unresolved_libraries"], 0)

    def test_cache_used_when_downloads_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "aaaa.debug").touch()
            with patch("collect_perf_debug.matching_debug", return_value=True):
                self.assertEqual(fetch_debug("aaaa", cache, [], []), cache / "aaaa.debug")

    def test_missing_debug_remains_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib").touch()
            info = dict(build_id="aaaa", dwarf=False, symbol_table=False, debug_link=None)
            with patch("collect_perf_debug.elf_info", return_value=info):
                result = collect(root, ["/lib"], root / "cache", [])
            self.assertEqual(result["libraries"]["/lib"]["status"], "missing-debuginfo")
            self.assertEqual(result["unresolved_libraries"], 1)

    def test_maps_select_executable_paths_and_decode_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            maps = Path(directory) / "maps"
            maps.write_text("1-2 r-xp 0 0:0 1 /some\\040lib.so\n"
                            "2-3 rw-p 0 0:0 1 /data\n"
                            "3-4 r-xp 0 0:0 1 /gone (deleted)\n")
            self.assertEqual(mapped_files([maps, maps]), ["/some lib.so"])

    def test_verified_debug_file_is_used_for_addr2line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "lib").touch()
            (root / "debug").touch()
            (root / ".debug-info.json").write_text(json.dumps({"libraries": {
                "/lib": {"status": "resolved", "debug_file": "/debug"}}}))
            self.assertEqual(resolve_arguments(["-e", "/lib", "-i"], str(root)),
                             ["-e", str(root / "debug"), "-i"])

    def test_named_leaf_does_not_hide_unknown_caller(self):
        result = summarize_visibility([
            "worker 1 1.0: 100 cpu-clock:\n",
            "  123 operator new+0x1 (/lib/allocator.so)\n",
            "  123 helper+0x1 (inlined)\n",
            "  456 [unknown] (/app)\n",
        ])
        self.assertEqual(result["unknown_leaf_samples"], 0)
        self.assertEqual(result["samples_with_unresolved_frames"], 1)
        self.assertEqual(result["unresolved_by_library"], {"/app": 1})
        self.assertEqual(result["inline_frames"], 1)


if __name__ == "__main__":
    unittest.main()
