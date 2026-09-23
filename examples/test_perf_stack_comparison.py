import io
import unittest

from compare_perf_stacks import compare, samples


def sample(frames="", period=100, time="1.000001"):
    return f"worker name 29 {time}: {period} cpu-clock:pppH: \n{frames}\n"


class StackComparisonTests(unittest.TestCase):
    def compare_text(self, old, new):
        return compare(samples(io.StringIO(old)), samples(io.StringIO(new)))

    def test_identical_samples(self):
        text = sample("\t1000 leaf+0x0 (/app)\n\t2000 caller+0x4 (/app)")
        result = self.compare_text(text, text)
        self.assertTrue(result["same_sample_sequence_and_weights"])
        self.assertEqual(result["counts"]["changed_physical_stacks"], 0)

    def test_dropped_empty_sample_is_not_hidden(self):
        result = self.compare_text(sample(), "")
        self.assertFalse(result["same_sample_sequence_and_weights"])
        self.assertEqual(result["counts"]["baseline_samples"], 1)

    def test_changed_period_is_detected(self):
        self.assertFalse(self.compare_text(sample(period=100), sample(period=101))[
            "same_sample_sequence_and_weights"])

    def test_changed_timestamp_is_detected(self):
        self.assertFalse(self.compare_text(sample(), sample(time="1.000002"))[
            "same_sample_sequence_and_weights"])

    def test_inline_frames_do_not_change_physical_depth(self):
        frames = "\t1000 leaf (/app)\n"
        result = self.compare_text(sample(frames), sample("\t1000 inline (inlined)\n" + frames))
        self.assertEqual(result["counts"]["changed_physical_stacks"], 0)

    def test_cpp_function_pointer_and_lambda_names_are_preserved(self):
        name = ("std::_Function_handler<void (Context, Payload<Result>), "
                "Handler::bind()::{lambda(Context, Payload<Result>)#1}>::"
                "_M_invoke(std::_Any_data const&, Context&&, Payload<Result>&&)+0x57")
        parsed = list(samples(io.StringIO(sample(f"\t1000 {name} (/usr/local/bin/service)"))))
        self.assertEqual(parsed[0].frames, [(0x1000, name, "/usr/local/bin/service")])

    def test_cpp_inline_frames_are_excluded(self):
        frames = "\t1000 leaf (/app)\n"
        inline = "\t1000 std::_Function_handler<void (int)>::call(int) (inlined)\n"
        result = self.compare_text(sample(frames), sample(inline + frames))
        self.assertEqual(result["counts"]["candidate_physical_frames"], 1)
        self.assertEqual(result["counts"]["changed_physical_stacks"], 0)

    def test_parentheses_in_dso_path_are_preserved(self):
        path = "/tmp/service (debug (symbols))/app"
        parsed = list(samples(io.StringIO(sample(f"\t1000 f(void (*)(int)) ({path})"))))
        self.assertEqual(parsed[0].frames, [(0x1000, "f(void (*)(int))", path)])

    def test_unbalanced_final_group_is_rejected(self):
        with self.assertRaises(ValueError):
            list(samples(io.StringIO(sample("\t1000 leaf (/tmp/app"))))

    def test_deleting_unknown_is_reported_as_shortening(self):
        result = self.compare_text(sample("\t1000 leaf (/app)\n\tdead [unknown] ([unknown])"),
                                   sample("\t1000 leaf (/app)"))
        self.assertEqual(result["counts"]["shorter_stacks"], 1)
        self.assertEqual(result["counts"]["baseline_unknown_frames"], 1)
        self.assertEqual(result["counts"]["candidate_unknown_frames"], 0)

    def test_lost_named_caller_is_reported(self):
        result = self.compare_text(sample("\t1000 leaf (/app)\n\t2000 caller (/app)"),
                                   sample("\t1000 leaf (/app)\n\t3000 other (/app)"))
        self.assertEqual(result["counts"]["stacks_losing_previously_named_addresses"], 1)

    def test_empty_stacks_are_counted(self):
        result = self.compare_text(sample(), sample())
        self.assertEqual(result["counts"]["candidate_empty_stacks"], 1)

    def test_unsupported_format_fails_instead_of_skipping(self):
        with self.assertRaises(ValueError):
            list(samples(io.StringIO("unexpected output\n")))

    def test_orphan_frame_fails(self):
        with self.assertRaises(ValueError):
            list(samples(io.StringIO("\t1000 leaf (/app)\n")))


if __name__ == "__main__":
    unittest.main()
