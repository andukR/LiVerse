import unittest

from bible_parser_core.live_pipeline import LiveReferencePipeline


class LiveReferencePipelineTest(unittest.TestCase):
    def assert_book_only_fragment_does_not_reuse_previous_numbers(self, fragment):
        with self.subTest(fragment=fragment):
            pipeline = LiveReferencePipeline()

            first = pipeline.process_text("иоана три шестнадцать")
            self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

            second = pipeline.process_text(fragment)
            self.assertFalse(second.get("matched"))
            self.assertEqual([fragment], second.get("vosk_buffer"))

    def test_bare_book_fragment_does_not_reuse_previous_numbers(self):
        for fragment in (
            "матфей",
            "паралипоменон",
            "коринфянам",
            "петра",
            "фессалоникийцам",
            "царств",
        ):
            self.assert_book_only_fragment_does_not_reuse_previous_numbers(fragment)

    def test_bare_book_fragment_can_start_next_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("матфей")
        self.assertFalse(first.get("matched"))
        self.assertEqual(["матфей"], first.get("vosk_buffer"))

        second = pipeline.process_text("третья глава шестнадцатый стих")
        self.assertEqual("Матфей 3:16", second.get("parsed", {}).get("ref"))

    def test_stale_buffer_does_not_repeat_previous_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("иоана три шестнадцать")
        self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("мих от до с ины")
        self.assertFalse(second.get("matched"))
        self.assertTrue(second.get("blocked_stale_repeat"))

    def test_stale_buffer_does_not_cascade_false_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("два же второе десятую притч")
        self.assertEqual("Притчи 2:2-10", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("четвертая из")
        self.assertFalse(second.get("matched"))
        self.assertTrue(second.get("blocked_stale_repeat"))


if __name__ == "__main__":
    unittest.main()
