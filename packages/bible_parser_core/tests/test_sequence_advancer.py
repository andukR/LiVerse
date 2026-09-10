from types import SimpleNamespace
import unittest

from bible_parser_core.sequence_advancer import (
    decide_sequence_advance,
    decide_sequence_advance_from_text,
)


class SequenceAdvancerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "book_id": 19,
            "current_index": 1,
            "targets": [
                {"start_chapter": 72, "start_verse": verse, "chapter": 72, "verse": verse}
                for verse in range(6, 13)
            ],
        }

    @staticmethod
    def candidate(verse: int, *, book_id: int = 19):
        return SimpleNamespace(
            reference=f"Пс. 72:{verse}",
            book_id=book_id,
            chapter=72,
            start_verse=verse,
            end_verse=verse,
        )

    def test_weak_next_verse_is_suggested_only_inside_known_sequence(self) -> None:
        decision = decide_sequence_advance(
            self.state,
            self.candidate(8),
            accepted=False,
            reason="score_below_threshold",
            score=48.442,
            margin=15.238,
            matched_words=4,
        )

        self.assertEqual("assisted_synchronize_forward", decision["action"])
        self.assertEqual(2, decision["target_index"])

    def test_two_words_may_advance_only_with_strong_nearby_evidence(self) -> None:
        accepted = decide_sequence_advance(
            self.state,
            self.candidate(8),
            accepted=False,
            reason="not_enough_matched_content_words",
            score=57.0,
            margin=20.0,
            matched_words=2,
        )
        rejected = decide_sequence_advance(
            self.state,
            self.candidate(8),
            accepted=False,
            reason="not_enough_matched_content_words",
            score=44.0,
            margin=55.0,
            matched_words=2,
        )

        self.assertEqual("assisted_synchronize_forward", accepted["action"])
        self.assertEqual("nearby_short_next_element", accepted["reason"])
        self.assertEqual("ignore", rejected["action"])

    def test_weak_distant_verse_does_not_jump_forward(self) -> None:
        decision = decide_sequence_advance(
            self.state,
            self.candidate(11),
            accepted=False,
            reason="score_below_threshold",
            score=60.0,
            margin=20.0,
            matched_words=5,
        )

        self.assertEqual("ignore", decision["action"])
        self.assertEqual("weak_distant_element", decision["reason"])

    def test_strong_later_verse_may_catch_up_without_moving_backward(self) -> None:
        forward = decide_sequence_advance(
            self.state,
            self.candidate(11),
            accepted=True,
            reason="immediate_strong_match",
            score=94.0,
            margin=40.0,
            matched_words=5,
        )
        backward = decide_sequence_advance(
            self.state,
            self.candidate(6),
            accepted=True,
            reason="immediate_strong_match",
            score=94.0,
            margin=40.0,
            matched_words=5,
        )

        self.assertEqual("synchronize_forward", forward["action"])
        self.assertEqual("ignore", backward["action"])
        self.assertEqual("automatic_backward_move_forbidden", backward["reason"])

    def test_candidate_from_other_book_is_ignored(self) -> None:
        decision = decide_sequence_advance(
            self.state,
            self.candidate(8, book_id=40),
            accepted=True,
            reason="immediate_strong_match",
            score=99.0,
            margin=60.0,
            matched_words=8,
        )

        self.assertEqual("ignore", decision["action"])
        self.assertEqual("outside_sequence_book", decision["reason"])

    def test_nearby_text_evidence_precedes_distant_global_candidate(self) -> None:
        scoped = SimpleNamespace(
            accepted=False, reason="score_below_threshold", score=48.0,
            margin=15.0, matched_words=4, top_candidate=self.candidate(8),
        )
        global_candidate = SimpleNamespace(
            accepted=True, reason="immediate_strong_match", score=95.0,
            margin=40.0, matched_words=7, top_candidate=self.candidate(11),
        )

        decision = decide_sequence_advance_from_text(
            self.state,
            global_candidate,
            scoped,
        )

        self.assertEqual("assisted_synchronize_forward", decision["action"])
        self.assertEqual(2, decision["target_index"])
        self.assertEqual("sequence_scoped", decision["evidence_source"])


if __name__ == "__main__":
    unittest.main()
