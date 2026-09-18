from types import SimpleNamespace
import unittest

from bible_parser_core.sequence_advancer import (
    decide_sequence_advance,
    decide_sequence_advance_from_text,
    decide_sequence_progress_from_text,
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
    def candidate(
        verse: int,
        *,
        book_id: int = 19,
        chapter: int = 72,
        ending_overlap_words: int = 0,
    ):
        return SimpleNamespace(
            reference=f"Пс. 72:{verse}",
            book_id=book_id,
            chapter=chapter,
            start_verse=verse,
            end_verse=verse,
            ending_overlap_words=ending_overlap_words,
        )

    def test_partial_match_of_current_verse_does_not_advance(self) -> None:
        decision = decide_sequence_advance(
            self.state,
            self.candidate(7),
            accepted=True,
            reason="immediate_strong_match",
            score=95.364,
            margin=81.048,
            matched_words=4,
        )

        self.assertEqual("keep", decision["action"])
        self.assertEqual("current_end_not_heard", decision["reason"])

    def test_strong_match_activates_initial_slide_before_its_end(self) -> None:
        state = {
            **self.state,
            "current_index": 0,
            "current_slide_visible": False,
        }
        decision = decide_sequence_advance(
            state,
            self.candidate(6),
            accepted=False,
            reason="score_below_threshold",
            score=72.507,
            margin=30.0,
            matched_words=5,
        )

        self.assertEqual("activate", decision["action"])
        self.assertEqual("assisted_initial_element", decision["reason"])

    def test_heard_end_of_current_verse_advances_to_next_known_verse(self) -> None:
        decision = decide_sequence_advance(
            self.state,
            self.candidate(7, ending_overlap_words=2),
            accepted=True,
            reason="immediate_strong_match",
            score=95.0,
            margin=40.0,
            matched_words=4,
        )

        self.assertEqual("advance", decision["action"])
        self.assertEqual("strong_current_boundary_auto_next", decision["reason"])
        self.assertEqual(2, decision["target_index"])

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
        self.assertEqual("required", decision["confirmation_state"])

    def test_weak_tail_cannot_cascade_after_assisted_advance(self) -> None:
        state = {
            "book_id": 21,
            "current_index": 2,
            "targets": [
                {"start_chapter": 2, "start_verse": verse, "chapter": 2, "verse": verse}
                for verse in range(18, 24)
            ],
        }

        def eccl(verse: int, *, ending_overlap_words: int = 0):
            return SimpleNamespace(
                reference=f"Еккл. 2:{verse}", book_id=21, chapter=2,
                start_verse=verse, end_verse=verse,
                ending_overlap_words=ending_overlap_words,
            )

        first = decide_sequence_advance(
            state, eccl(20, ending_overlap_words=2), accepted=False,
            reason="score_below_threshold", score=72.299, margin=54.914,
            matched_words=7,
        )
        self.assertEqual("assisted_advance", first["action"])
        self.assertEqual(3, first["target_index"])
        self.assertEqual("required", first["confirmation_state"])

        # Replay's state update after the first decision is represented here.
        state["current_index"] = first["target_index"]
        state["await_current_confirmation"] = True
        weak_tail = decide_sequence_advance(
            state, eccl(22), accepted=False, reason="score_below_threshold",
            score=52.152, margin=29.584, matched_words=3,
        )
        self.assertEqual("keep", weak_tail["action"])
        self.assertEqual("await_current_element_confirmation", weak_tail["reason"])

        confirmed_current = decide_sequence_advance(
            state, eccl(21), accepted=True, reason="immediate_strong_match",
            score=91.0, margin=40.0, matched_words=5,
        )
        self.assertEqual("keep", confirmed_current["action"])
        self.assertEqual("confirmed", confirmed_current["confirmation_state"])

        # After the current verse is confirmed, normal progression resumes.
        state.pop("await_current_confirmation")
        next_verse = decide_sequence_advance(
            state, eccl(22), accepted=False, reason="score_below_threshold",
            score=52.152, margin=29.584, matched_words=3,
        )
        self.assertEqual("assisted_synchronize_forward", next_verse["action"])

    def test_short_strong_tail_cannot_cascade_after_assisted_advance(self) -> None:
        state = {
            "book_id": 21,
            "current_index": 1,
            "await_current_confirmation": True,
            "targets": [
                {"start_chapter": 4, "start_verse": verse, "chapter": 4, "verse": verse}
                for verse in range(3, 7)
            ],
        }

        # This is a strong-looking result for the verse just shown, but it is
        # still only a short tail of the same recognition window.
        short_tail = decide_sequence_advance(
            state,
            self.candidate(4, book_id=21, chapter=4, ending_overlap_words=2),
            accepted=True,
            reason="immediate_strong_match",
            score=96.0,
            margin=60.0,
            matched_words=4,
        )
        self.assertEqual("keep", short_tail["action"])
        self.assertEqual("await_current_element_confirmation", short_tail["reason"])

        fresh_end = decide_sequence_advance(
            state,
            self.candidate(4, book_id=21, chapter=4, ending_overlap_words=2),
            accepted=True,
            reason="immediate_strong_match",
            score=96.0,
            margin=60.0,
            matched_words=5,
        )
        self.assertEqual("advance", fresh_end["action"])
        self.assertEqual(2, fresh_end["target_index"])

    def test_short_strong_tail_cannot_cascade_after_strong_next_sync(self) -> None:
        state = {
            "book_id": 21,
            "current_index": 1,
            "targets": [
                {"start_chapter": 4, "start_verse": verse, "chapter": 4, "verse": verse}
                for verse in range(3, 7)
            ],
        }
        synced = decide_sequence_advance(
            state,
            self.candidate(5, book_id=21, chapter=4),
            accepted=True,
            reason="immediate_strong_match",
            score=96.0,
            margin=60.0,
            matched_words=6,
        )
        self.assertEqual("synchronize_forward", synced["action"])
        self.assertEqual("required", synced["confirmation_state"])

        state["current_index"] = synced["target_index"]
        state["await_current_confirmation"] = True
        short_tail = decide_sequence_advance(
            state,
            self.candidate(5, book_id=21, chapter=4, ending_overlap_words=2),
            accepted=True,
            reason="immediate_strong_match",
            score=96.0,
            margin=60.0,
            matched_words=4,
        )
        self.assertEqual("keep", short_tail["action"])
        self.assertEqual("await_current_element_confirmation", short_tail["reason"])

    def test_strong_local_next_match_can_recover_while_confirmation_is_pending(self) -> None:
        state = {
            "book_id": 21,
            "current_index": 1,
            "await_current_confirmation": True,
            "targets": [
                {"start_chapter": 4, "start_verse": verse, "chapter": 4, "verse": verse}
                for verse in range(3, 7)
            ],
        }
        recovery = decide_sequence_advance(
            state,
            self.candidate(5, book_id=21, chapter=4),
            accepted=False,
            reason="score_below_threshold",
            score=62.599,
            margin=47.074,
            matched_words=8,
        )
        self.assertEqual("assisted_synchronize_forward", recovery["action"])
        self.assertEqual(2, recovery["target_index"])
        self.assertEqual("required", recovery["confirmation_state"])

        weak_recovery = decide_sequence_advance(
            state,
            self.candidate(5, book_id=21, chapter=4),
            accepted=False,
            reason="score_below_threshold",
            score=62.599,
            margin=47.074,
            matched_words=4,
        )
        self.assertEqual("keep", weak_recovery["action"])
        self.assertEqual("await_current_element_confirmation", weak_recovery["reason"])

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

    def test_active_range_can_recover_strong_later_verse_after_skipped_text(self) -> None:
        state = {**self.state, "allow_distant_skip": True}
        decision = decide_sequence_advance(
            state,
            self.candidate(11),
            accepted=False,
            reason="duplicate_cooldown",
            score=91.0,
            margin=59.0,
            matched_words=7,
        )

        self.assertEqual("synchronize_forward", decision["action"])
        self.assertEqual("strong_later_element_skip", decision["reason"])
        self.assertEqual(5, decision["target_index"])
        self.assertEqual([2, 3, 4], decision["skipped_indices"])
        self.assertEqual("required", decision["confirmation_state"])

        state["current_index"] = decision["target_index"]
        state["await_current_confirmation"] = True
        repeated = decide_sequence_advance(
            state,
            self.candidate(11),
            accepted=False,
            reason="duplicate_cooldown",
            score=91.0,
            margin=59.0,
            matched_words=7,
        )
        self.assertEqual("keep", repeated["action"])
        self.assertEqual("await_recovered_element_confirmation", repeated["reason"])

        state["recovered_element_pending"] = True
        repeated_after_recovery = decide_sequence_advance(
            state,
            self.candidate(11),
            accepted=True,
            reason="immediate_strong_match",
            score=94.0,
            margin=40.0,
            matched_words=5,
        )
        self.assertEqual("keep", repeated_after_recovery["action"])
        self.assertEqual("await_recovered_element_confirmation", repeated_after_recovery["reason"])

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

    def test_one_window_advances_only_one_intermediate_verse(self) -> None:
        decisions = {
            1: SimpleNamespace(
                accepted=False, reason="score_below_threshold", score=55.0,
                margin=20.0, matched_words=6, top_candidate=self.candidate(8),
            ),
            2: SimpleNamespace(
                accepted=False, reason="not_enough_matched_content_words", score=71.0,
                margin=55.0, matched_words=2, top_candidate=self.candidate(9),
            ),
            3: SimpleNamespace(
                accepted=False, reason="score_below_threshold", score=20.0,
                margin=2.0, matched_words=1, top_candidate=self.candidate(9),
            ),
        }

        decision, evidence = decide_sequence_progress_from_text(
            self.state,
            None,
            lambda state: decisions[int(state["current_index"])],
        )

        self.assertEqual("assisted_synchronize_forward", decision["action"])
        self.assertEqual("assisted_next_element", decision["reason"])
        self.assertEqual(2, decision["target_index"])
        self.assertNotIn("sequence_steps", decision)
        self.assertIs(evidence, decisions[1])


if __name__ == "__main__":
    unittest.main()
