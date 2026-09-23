import tempfile
import unittest
from pathlib import Path

from perf_addr2line import resolve_arguments
from validate_perf_script import summarize


class PerfSymbolizationTests(unittest.TestCase):
    def test_rebases_stripped_library_and_preserves_other_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library = root / "usr/local/lib/servicegen/libc.so.6"
            library.parent.mkdir(parents=True)
            library.touch()
            self.assertEqual(
                resolve_arguments(["-e", "/usr/local/lib/servicegen/libc.so.6", "-i", "-f"], str(root)),
                ["-e", str(library), "-i", "-f"],
            )

    def test_already_prefixed_binary_and_long_option(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            binary = root / "service"
            binary.touch()
            self.assertEqual(resolve_arguments(["--exe=" + str(binary)], str(root)), ["--exe=" + str(binary)])

    def test_missing_file_is_not_resolved_against_profiler_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                resolve_arguments(["-e", "/bin/sh"], directory)

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                resolve_arguments(["-e", "/../"], directory)

    def test_thread_only_unknown_and_empty_profiles_have_no_functions(self):
        for lines in ([], ["worker 1 123.0: 99 cpu-clock:\n"], ["\tffff800012345678 [unknown] ([unknown])\n"]):
            self.assertEqual(summarize(lines)["named_function_frames"], 0)

    def test_cpp_names_are_kept_and_dso_labels_are_not_functions(self):
        result = summarize([
            "worker 1 123.0: 99 cpu-clock:\n",
            "\t1234 std::function<void (int)>::operator()+0x4 (/app/service)\n",
            "\t5678 [libc.so.6] (/usr/lib/libc.so.6)\n",
            "\tffff800012345678 [unknown] ([unknown])\n",
        ])
        self.assertEqual(result, {"frames": 3, "named_function_frames": 1, "named_user_function_frames": 1, "unresolved_frames": 2})

    def test_kernel_symbols_cannot_hide_missing_application_symbols(self):
        result = summarize(["\tffff8000801646cc __seccomp_filter+0x94 ([kernel.kallsyms])\n"])
        self.assertEqual(result["named_function_frames"], 1)
        self.assertEqual(result["named_user_function_frames"], 0)


if __name__ == "__main__":
    unittest.main()
