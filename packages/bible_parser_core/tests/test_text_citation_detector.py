from __future__ import annotations

import unittest
import tempfile
import argparse
from pathlib import Path
from types import SimpleNamespace

from bible_parser_core.bible_text_search import BibleTextSearchResult
from bible_parser_core.text_citation_detector import (
    ScriptureTextDetector,
    SlidingSpeechBuffer,
    TextDetectionConfig,
)


def hit(
    reference: str,
    score: float,
    *,
    matched: tuple[str, ...],
    ordered: float = 85.0,
    bigram: float = 50.0,
    trigram: float = 25.0,
    book_id: int = 43,
    chapter: int = 3,
    verse: int = 16,
) -> BibleTextSearchResult:
    return BibleTextSearchResult(
        reference=reference,
        text="текст стиха",
        score=score,
        coverage=85.0,
        ordered_similarity=ordered,
        token_similarity=85.0,
        bigram_overlap=bigram,
        trigram_overlap=trigram,
        matched_lemmas=matched,
        book_id=book_id,
        chapter=chapter,
        start_verse=verse,
        end_verse=verse,
    )


class FakeSearcher:
    def __init__(self, result_batches: list[list[BibleTextSearchResult]]) -> None:
        self.result_batches = list(result_batches)

    def search(self, text: str, **_unused: object):
        results = self.result_batches.pop(0)
        return text.split(), results


class SlidingSpeechBufferTest(unittest.TestCase):
    def test_quote_split_between_final_results_forms_search_window(self) -> None:
        buffer = SlidingSpeechBuffer(buffer_words=20, window_sizes=(5, 7, 10), min_words=5)

        self.assertEqual([], buffer.add("ибо так"))
        windows = buffer.add("возлюбил Бог мир")

        self.assertEqual([5], [window.size for window in windows])
        self.assertEqual("ибо так возлюбил бог мир", windows[0].text)

    def test_buffer_keeps_only_configured_number_of_recent_words(self) -> None:
        buffer = SlidingSpeechBuffer(buffer_words=7, window_sizes=(5, 7), min_words=5)

        windows = buffer.add("один два три четыре пять шесть семь восемь")

        self.assertEqual(("два", "три", "четыре", "пять", "шесть", "семь", "восемь"), buffer.tokens)
        self.assertEqual("четыре пять шесть семь восемь", windows[0].text)
        self.assertEqual("два три четыре пять шесть семь восемь", windows[1].text)

    def test_complete_vosk_fragment_is_also_a_window(self) -> None:
        buffer = SlidingSpeechBuffer(buffer_words=20, window_sizes=(5, 7, 10, 15), min_words=5)

        windows = buffer.add("один два три четыре пять шесть")

        self.assertEqual([5, 6], [window.size for window in windows])
        self.assertEqual("один два три четыре пять шесть", windows[1].text)

    def test_clear_removes_previous_sermon_context(self) -> None:
        buffer = SlidingSpeechBuffer(buffer_words=10, window_sizes=(5,), min_words=5)
        buffer.add("ибо так возлюбил бог мир")

        buffer.clear()

        self.assertEqual((), buffer.tokens)
        self.assertEqual([], buffer.add("новая короткая фраза"))

    def test_invalid_window_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SlidingSpeechBuffer(buffer_words=5, window_sizes=(7,), min_words=5)


class ScriptureTextDetectorTest(unittest.TestCase):
    def config(self, **overrides: object) -> TextDetectionConfig:
        values = {
            "buffer_words": 20,
            "window_sizes": (5,),
            "min_words": 5,
            "search_interval_ms": 0,
            "immediate_score": 99.0,
        }
        values.update(overrides)
        return TextDetectionConfig(**values)

    def test_strong_candidate_requires_two_different_overlapping_windows(self) -> None:
        first = hit(
            "Ин. 3:16", 82.0,
            matched=("ибо", "так", "возлюбить", "бог", "мир", "сын", "свой"),
        )
        second = hit("1Ин. 4:9", 60.0, matched=("бог", "мир"), bigram=0.0, trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[first, second], [first, second]]),
            self.config(),
        )

        pending = detector.process_fragment("ибо так возлюбить бог мир", now=0.0)
        accepted = detector.process_fragment("сын свой", now=0.8)

        self.assertFalse(pending.accepted)
        self.assertEqual("pending_confirmation", pending.reason)
        self.assertTrue(accepted.accepted)
        self.assertEqual("Ин. 3:16", accepted.reference)
        self.assertEqual("confirmed_stable_match", accepted.reason)

    def test_very_strong_exact_candidate_can_be_accepted_immediately(self) -> None:
        strong = hit(
            "Ин. 1:1", 96.0,
            matched=("начало", "слово", "пребывать", "бог", "жизнь", "истина"),
            trigram=75.0,
        )
        other = hit("Ин. 1:2", 70.0, matched=("слово", "бог"), trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[strong, other]]),
            self.config(immediate_score=90.0, window_sizes=(6,)),
        )

        decision = detector.process_fragment("начало слово пребывать бог жизнь истина", now=0.0)

        self.assertTrue(decision.accepted)
        self.assertEqual("immediate_strong_match", decision.reason)

    def test_repeated_content_word_counts_as_repeated_evidence(self) -> None:
        strong = hit(
            "Ин. 1:1", 95.0,
            matched=("слово", "слово", "бог"),
            ordered=96.0,
            trigram=60.0,
        )
        other = hit("Ин. 1:2", 70.0, matched=("слово", "бог"), trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[strong, other]]),
            self.config(immediate_score=90.0, window_sizes=(6,)),
        )

        decision = detector.process_fragment("слово и слово было у бог", now=0.0)

        self.assertTrue(decision.accepted)
        self.assertEqual(3, decision.matched_words)

    def test_two_verse_range_tolerates_limited_vosk_distortion(self) -> None:
        verse_range = hit(
            "Пс. 22:1-2", 63.6,
            matched=("ни", "чем", "нуждаться", "злачных"),
            ordered=69.8,
            bigram=64.3,
            trigram=53.8,
            book_id=19,
            chapter=22,
            verse=1,
        )
        verse_range = BibleTextSearchResult(
            **{**verse_range.__dict__, "end_verse": 2}
        )
        single = hit(
            "Пс. 22:1", 45.9,
            matched=("нуждаться", "я"),
            bigram=35.0,
            trigram=20.0,
            book_id=19,
            chapter=22,
            verse=1,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[verse_range, single]]),
            self.config(window_sizes=(14,)),
        )

        decision = detector.process_fragment(
            "я ни в чем не буду нуждаться он покойник меня на злачных паша тех",
            now=0.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Пс. 22:1-2", decision.reference)
        self.assertEqual("immediate_strong_range_match", decision.reason)

    def test_broader_three_verse_range_wins_over_stronger_contained_suffix(self) -> None:
        short_range = hit(
            "Лк. 14:29-30", 83.6,
            matched=("смеяться", "над", "они", "говорить", "человек"),
            bigram=65.0,
            trigram=55.0,
            book_id=42,
            chapter=14,
            verse=29,
        )
        short_range = BibleTextSearchResult(
            **{**short_range.__dict__, "end_verse": 30}
        )
        broad_range = hit(
            "Лк. 14:28-30", 80.5,
            matched=("построить", "башня", "издержка", "основание", "смеяться"),
            bigram=69.0,
            trigram=58.0,
            book_id=42,
            chapter=14,
            verse=28,
        )
        broad_range = BibleTextSearchResult(
            **{**broad_range.__dict__, "end_verse": 30}
        )
        contained = BibleTextSearchResult(
            **{**broad_range.__dict__, "reference": "Лк. 14:28-29", "score": 76.7, "end_verse": 29}
        )
        unrelated = hit(
            "Есф. 4:17", 28.0,
            matched=("человек",),
            bigram=0.0,
            trigram=0.0,
            book_id=17,
            chapter=4,
            verse=17,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([
                [short_range, unrelated],
                [broad_range, contained, short_range, unrelated],
            ]),
            self.config(window_sizes=(5, 10), buffer_words=10),
        )

        decision = detector.process_fragment(
            "построить башня считать издержка основание смеяться над они говорить человек",
            now=0.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Лк. 14:28-30", decision.reference)

    def test_weak_two_verse_range_does_not_use_relaxed_range_rule(self) -> None:
        weak_range = hit(
            "Пс. 22:1-2", 65.0,
            matched=("обычный", "фраза", "похожий", "слово"),
            bigram=35.0,
            trigram=20.0,
            book_id=19,
            chapter=22,
            verse=1,
        )
        weak_range = BibleTextSearchResult(
            **{**weak_range.__dict__, "end_verse": 2}
        )
        other = hit("Пс. 22:1", 48.0, matched=("нуждаться", "я"))
        detector = ScriptureTextDetector(
            FakeSearcher([[weak_range, other]]),
            self.config(window_sizes=(7,)),
        )

        decision = detector.process_fragment(
            "обычная фраза с несколькими похожими словами подряд",
            now=0.0,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("score_below_threshold", decision.reason)

    def test_ordinary_biblical_vocabulary_is_rejected(self) -> None:
        weak = hit(
            "Ин. 3:16", 58.0,
            matched=("бог", "любить", "человек"),
            ordered=45.0,
            bigram=0.0,
            trigram=0.0,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[weak]]),
            self.config(window_sizes=(6,)),
        )

        decision = detector.process_fragment("бог хотеть чтобы мы любить человек", now=0.0)

        self.assertFalse(decision.accepted)
        self.assertEqual("score_below_threshold", decision.reason)

    def test_recently_shown_reference_is_suppressed(self) -> None:
        strong = hit(
            "Ин. 3:16", 96.0,
            matched=("ибо", "возлюбить", "бог", "мир", "сын"),
            trigram=75.0,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[strong], [strong]]),
            self.config(immediate_score=90.0),
        )

        self.assertTrue(detector.process_fragment("ибо возлюбить бог мир сын", now=0.0).accepted)
        duplicate = detector.process_fragment("единородный дать нам жизнь", now=1.0)

        self.assertFalse(duplicate.accepted)
        self.assertEqual("duplicate_cooldown", duplicate.reason)

    def test_explicit_address_temporarily_suppresses_text_detection(self) -> None:
        detector = ScriptureTextDetector(FakeSearcher([]), self.config())
        detector.suppress_after_address("Ин. 3:16", now=2.0)

        decision = detector.process_fragment("ибо так возлюбил бог мир", now=3.0)

        self.assertFalse(decision.accepted)
        self.assertEqual("address_suppression", decision.reason)

    def test_full_and_abbreviated_reference_share_duplicate_cooldown(self) -> None:
        strong = hit(
            "Иак. 1:26", 96.0,
            matched=("думать", "благочестивый", "обуздывать", "язык", "сердце"),
            trigram=75.0,
            book_id=59,
            chapter=1,
            verse=26,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[strong]]),
            self.config(immediate_score=90.0),
        )
        detector.suppress_after_address("Иаков 1:26", now=0.0)

        duplicate = detector.process_fragment(
            "думать благочестивый обуздывать язык сердце",
            now=9.0,
        )

        self.assertFalse(duplicate.accepted)
        self.assertEqual("duplicate_cooldown", duplicate.reason)


class ReplayTranscriptTest(unittest.TestCase):
    def test_jsonl_fragments_are_replayed_in_order(self) -> None:
        from tools.replay_transcript import replay_transcript

        class RecordingDetector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float]] = []

            def process_fragment(self, text: str, now: float):
                self.calls.append((text, now))
                return SimpleNamespace(
                    accepted=False,
                    reference=None,
                    score=0.0,
                    margin=0.0,
                    matched_words=0,
                    window_text="",
                    reason="not_enough_words",
                    confirmations=0,
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.jsonl"
            path.write_text(
                '# comment\n{"time": 0.0, "text": "ибо так"}\n'
                '{"time": 0.8, "text": "возлюбил бог мир"}\n',
                encoding="utf-8",
            )
            detector = RecordingDetector()
            rows = replay_transcript(path, detector)  # type: ignore[arg-type]

        self.assertEqual(
            [("ибо так", 0.0), ("возлюбил бог мир", 0.8)],
            detector.calls,
        )
        self.assertEqual(2, len(rows))

    def test_invalid_jsonl_reports_line_number(self) -> None:
        from tools.replay_transcript import load_transcript

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.jsonl"
            path.write_text('{"time": "bad", "text": "test"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "строка 1"):
                load_transcript(path)


class TextCitationIntegrationTest(unittest.TestCase):
    def test_startup_explains_how_to_install_missing_text_database(self) -> None:
        from tools.vosk_grammar_probe import text_detection_database_startup_message

        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "bible_index" / "bible_index.db"
            message = text_detection_database_startup_message(
                "hybrid_confirm",
                database_path,
            )

        self.assertIn("база поиска цитат по тексту не найдена", message)
        self.assertIn(str(database_path), message)
        self.assertIn("LIVERSE_TEXT_DETECTION_DB", message)

    def test_startup_database_notice_is_not_shown_when_unneeded_or_installed(self) -> None:
        from tools.vosk_grammar_probe import text_detection_database_startup_message

        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "bible_index.db"
            self.assertEqual(
                "",
                text_detection_database_startup_message("address_only", database_path),
            )
            database_path.touch()
            self.assertEqual(
                "",
                text_detection_database_startup_message("hybrid_confirm", database_path),
            )

    def test_replay_summary_labels_address_and_text_detection(self) -> None:
        from tools.replay_audio_files import citation_summary_lines

        lines = citation_summary_lines([
            {
                "timecode": "00:01:02.000",
                "ref": "Иоанн 3:16",
                "payload": {"source": "parser"},
            },
            {
                "timecode": "00:02:03.000",
                "ref": "Матфей 7:21",
                "payload": {"source": "text_citation"},
            },
            {
                "timecode": "00:03:04.000",
                "ref": "Иаков 1:27",
                "payload": {"source": "context_range"},
            },
        ])

        self.assertTrue(lines[0].startswith(
            "1. 00:01:02.000  Иоанн 3:16 — по адресу\n   Текст:"
        ))
        self.assertIn("Ибо так возлюбил Бог мир", lines[0])
        self.assertTrue(lines[1].startswith(
            "2. 00:02:03.000  Матфей 7:21 — по тексту\n   Текст:"
        ))
        self.assertTrue(lines[2].startswith(
            "3. 00:03:04.000  Иаков 1:27 — по адресу\n   Текст:"
        ))

    def test_replay_long_passage_waits_for_its_final_verse(self) -> None:
        from tools.replay_audio_files import replay_long_passage, replay_long_passage_match

        passage = replay_long_passage({
            "parsed": {
                "book": "Иаков",
                "chapter": 1,
                "start_verse": 19,
                "end_chapter": 1,
                "end_verse": 27,
                "ref": "Иаков 1:19-27",
            },
        })
        self.assertIsNotNone(passage)

        verse_26 = hit(
            "Иак. 1:26", 80.0, matched=("язык",),
            book_id=59, chapter=1, verse=26,
        )
        verse_27 = hit(
            "Иак. 1:27", 80.0, matched=("благочестие",),
            book_id=59, chapter=1, verse=27,
        )
        decision_26 = SimpleNamespace(
            accepted=False, reason="pending_confirmation", top_candidate=verse_26,
        )
        decision_27 = SimpleNamespace(
            accepted=False, reason="pending_confirmation", top_candidate=verse_27,
        )

        self.assertFalse(replay_long_passage_match(decision_26, passage)["completed"])
        self.assertTrue(replay_long_passage_match(decision_27, passage)["completed"])

    def test_pending_candidate_can_advance_known_long_passage_boundary(self) -> None:
        from tools.vosk_grammar_probe import text_decision_ready_for_scripture_range

        candidate = hit("1Ин. 2:6", 76.089, matched=("говорить", "пребывать", "поступать"))
        decision = SimpleNamespace(
            accepted=False,
            top_candidate=candidate,
            reason="pending_confirmation",
        )

        self.assertTrue(text_decision_ready_for_scripture_range(decision))

    def test_weak_candidate_cannot_advance_known_long_passage_boundary(self) -> None:
        from tools.vosk_grammar_probe import text_decision_ready_for_scripture_range

        candidate = hit("1Ин. 2:6", 50.0, matched=("поступать",))
        decision = SimpleNamespace(
            accepted=False,
            top_candidate=candidate,
            reason="score_below_threshold",
        )

        self.assertFalse(text_decision_ready_for_scripture_range(decision))

    def test_search_result_becomes_canonical_existing_slide_payload(self) -> None:
        from tools.vosk_grammar_probe import text_citation_payload

        candidate = hit(
            "Ин. 3:16", 94.0,
            matched=("возлюбить", "бог", "мир"),
        )
        decision = SimpleNamespace(
            top_candidate=candidate,
            window_text="ибо так возлюбил бог мир",
            score=94.0,
            margin=20.0,
            matched_words=3,
            confirmations=1,
            reason="immediate_strong_match",
        )

        payload = text_citation_payload(decision, "ибо так возлюбил бог мир")

        self.assertEqual("Иоанн 3:16", payload["parsed"]["ref"])
        self.assertEqual("Иоанн 3:16", payload["slide"]["ref"])
        self.assertEqual("vosk:text_citation", payload["slide"]["source"])

    def test_hybrid_modes_reuse_existing_approval_policy(self) -> None:
        from tools.vosk_grammar_probe import text_citation_output_args

        base = argparse.Namespace(require_approval=True, semi_auto_approval=True)
        base.citation_detection_mode = "hybrid_auto"
        automatic = text_citation_output_args(base)
        base.citation_detection_mode = "hybrid_confirm"
        confirmed = text_citation_output_args(base)

        self.assertFalse(automatic.require_approval)
        self.assertFalse(automatic.semi_auto_approval)
        self.assertTrue(confirmed.require_approval)
        self.assertFalse(confirmed.semi_auto_approval)


if __name__ == "__main__":
    unittest.main()
