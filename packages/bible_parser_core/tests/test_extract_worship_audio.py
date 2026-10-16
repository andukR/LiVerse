import unittest

from tools.extract_worship_audio import singing_blocks


def singing(start, end, speech=0.05, voice=0.2):
    return dict(start=start, end=end, music=0.9, singing=voice, speech=speech)


class WorshipBoundaryTests(unittest.TestCase):
    def blocks(self, rows):
        return singing_blocks(rows, 0.65, 0.05, 0.6, 120, 20)

    def test_prayer_between_songs_is_preserved(self):
        self.assertEqual(self.blocks([
            singing(0, 30), singing(30, 60, speech=0.9, voice=0.001),
            singing(60, 90)]), [[0, 90]])

    def test_final_prayer_with_guitar_is_excluded(self):
        self.assertEqual(self.blocks([
            singing(200, 750), singing(750, 810, speech=0.8, voice=0.001)]),
            [[200, 750]])

    def test_long_prayer_separates_blocks(self):
        self.assertEqual(self.blocks([singing(0, 30), singing(151, 181)]),
                         [[0, 30], [151, 181]])

    def test_isolated_false_detection_does_not_extend_end(self):
        self.assertEqual(self.blocks([singing(0, 30), singing(100, 105)]), [[0, 30]])

    def test_instrumental_music_does_not_count_as_singing(self):
        self.assertEqual(self.blocks([singing(0, 30, voice=0.001)]), [])


if __name__ == "__main__":
    unittest.main()
