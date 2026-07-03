import unittest

from bible_parser_core.live_pipeline import LiveReferencePipeline


class LiveReferencePipelineTest(unittest.TestCase):
    def test_bare_paralipomenon_does_not_reuse_previous_numbers(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("иоана три шестнадцать")
        self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("паралипоменон")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["паралипоменон"], second.get("vosk_buffer"))


if __name__ == "__main__":
    unittest.main()
