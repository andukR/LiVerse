from __future__ import annotations

import unittest
import tempfile
import argparse
import json
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
    end_verse: int | None = None,
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
        end_verse=end_verse or verse,
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
            FakeSearcher([[first, second], [first, second], []]),
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

    def test_exact_five_word_verse_prefix_is_accepted_immediately(self) -> None:
        john_316 = BibleTextSearchResult(
            reference="Ин. 3:16",
            text=(
                "Ибо так возлюбил Бог мир, что отдал Сына Своего Единородного, "
                "дабы всякий верующий в Него не погиб, но имел жизнь вечную."
            ),
            score=88.94,
            coverage=100.0,
            ordered_similarity=34.97,
            token_similarity=100.0,
            bigram_overlap=100.0,
            trigram_overlap=100.0,
            matched_lemmas=("ибо", "так", "возлюбить", "бог", "мир"),
            book_id=43,
            chapter=3,
            start_verse=16,
            end_verse=16,
        )
        other = hit(
            "1Ин. 4:11",
            54.92,
            matched=("так", "возлюбить", "бог"),
            bigram=25.0,
            trigram=0.0,
            book_id=62,
            chapter=4,
            verse=11,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[john_316, other]]),
            self.config(),
        )

        decision = detector.process_fragment("ибо так возлюбить бог мир", now=0.0)

        self.assertTrue(decision.accepted)
        self.assertEqual("Ин. 3:16", decision.reference)
        self.assertEqual("immediate_exact_phrase_match", decision.reason)

    def test_exact_complete_two_word_verse_is_accepted_immediately(self) -> None:
        john_1135 = BibleTextSearchResult(
            reference="Ин. 11:35",
            text="Иисус прослезился.",
            score=100.0,
            coverage=100.0,
            ordered_similarity=100.0,
            token_similarity=100.0,
            bigram_overlap=100.0,
            trigram_overlap=0.0,
            matched_lemmas=("иисус", "прослезиться"),
            book_id=43,
            chapter=11,
            start_verse=35,
            end_verse=35,
        )
        other = hit(
            "Нав. 4:15",
            29.14,
            matched=("иисус",),
            ordered=25.0,
            bigram=0.0,
            trigram=0.0,
            book_id=6,
            chapter=4,
            verse=15,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[], [], [john_1135, other]]),
            self.config(),
        )
        detector.process_fragment("обычная речь перед коротким стихом", now=0.0)

        decision = detector.process_fragment("иисус прослезиться", now=1.0)

        self.assertTrue(decision.accepted)
        self.assertEqual("Ин. 11:35", decision.reference)
        self.assertEqual("immediate_exact_short_verse_match", decision.reason)

    def test_unrelated_two_word_fragment_is_not_accepted(self) -> None:
        weak = hit(
            "Нав. 4:15",
            55.0,
            matched=("иисус",),
            ordered=40.0,
            bigram=0.0,
            trigram=0.0,
            book_id=6,
            chapter=4,
            verse=15,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[weak]]),
            self.config(),
        )

        decision = detector.process_fragment("иисус сказал", now=0.0)

        self.assertFalse(decision.accepted)

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

    def test_broader_range_wins_when_strong_suffix_leads_by_under_ten_points(self) -> None:
        suffix = hit(
            "Флп. 3:9", 92.686,
            matched=("через", "веру", "христос", "праведность", "бог"),
            bigram=70.0,
            trigram=55.0,
            book_id=50,
            chapter=3,
            verse=9,
        )
        full_range = hit(
            "Флп. 3:8-9", 83.844,
            matched=("приобрести", "христос", "найтись", "праведность", "вера"),
            bigram=65.0,
            trigram=50.0,
            book_id=50,
            chapter=3,
            verse=8,
        )
        full_range = BibleTextSearchResult(**{**full_range.__dict__, "end_verse": 9})
        detector = ScriptureTextDetector(
            FakeSearcher([[suffix], [full_range]]),
            self.config(window_sizes=(5, 10), buffer_words=10, immediate_score=90.0),
        )

        decision = detector.process_fragment(
            "приобрести христос найтись праведность вера через христос праведность бог по вере",
            now=0.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Флп. 3:8-9", decision.reference)

    def test_strong_full_range_beats_much_cleaner_last_verse_suffix(self) -> None:
        suffix = hit(
            "1Фес. 5:5", 91.62,
            matched=("ибо", "все", "свет", "сыны", "дня"),
            bigram=70.0,
            trigram=55.0,
            book_id=52,
            chapter=5,
            verse=5,
        )
        full_range = hit(
            "1Фес. 5:4-5", 72.14,
            matched=("тьма", "чтобы", "тать", "ибо", "сыны"),
            bigram=61.9,
            trigram=45.0,
            book_id=52,
            chapter=5,
            verse=4,
            end_verse=5,
        )
        unrelated = hit("Есф. 8:12", 40.0, matched=("вы",), bigram=0.0, trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[suffix, unrelated], [full_range, unrelated]]),
            self.config(window_sizes=(5, 10), buffer_words=10, immediate_score=90.0),
        )

        decision = detector.process_fragment(
            "но вы братья не во тьме чтобы татья ибо все вы сыны дня",
            now=0.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("1Фес. 5:4-5", decision.reference)

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
            FakeSearcher([[strong], [strong], []]),
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

    def test_text_range_suppresses_contained_repeat_and_accepts_next_verse(self) -> None:
        repeated = hit(
            "Еф. 3:18", 91.0,
            matched=("широта", "долгота", "глубина", "высота"),
            trigram=75.0,
            book_id=49,
            chapter=3,
            verse=18,
        )
        following = hit(
            "Еф. 3:19", 77.0,
            matched=("уразуметь", "превосходящий", "разумение", "любовь", "христов"),
            bigram=50.0,
            trigram=25.0,
            book_id=49,
            chapter=3,
            verse=19,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[repeated], [following]]),
            self.config(immediate_score=99.0),
        )
        detector.mark_shown("Ефесянам 3:17-18", now=0.0)

        duplicate = detector.process_fragment("широта долгота глубина высота любовь", now=1.0)
        continuation = detector.process_fragment(
            "уразуметь превосходящий разумение любовь христов", now=2.0,
        )

        self.assertFalse(duplicate.accepted)
        self.assertEqual("duplicate_cooldown", duplicate.reason)
        self.assertTrue(continuation.accepted)
        self.assertEqual("Еф. 3:19", continuation.reference)
        self.assertEqual("continuation_after_shown_range", continuation.reason)

    def test_expanded_range_never_auto_narrows_to_a_verse_from_older_range(self) -> None:
        verse = hit(
            "Флп. 3:15", 95.0,
            matched=("мыслить", "бог", "открыть"),
            trigram=75.0,
            book_id=50,
            chapter=3,
            verse=15,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[verse]]),
            self.config(immediate_score=99.0),
        )
        detector.mark_shown("Филиппийцам 3:13-14", now=0.0)
        detector.mark_shown("Филиппийцам 3:13-15", now=1.0)

        decision = detector.process_fragment("мыслить бог это открыть любовь", now=2.0)

        self.assertFalse(decision.accepted)
        self.assertEqual("duplicate_cooldown", decision.reason)

    def test_expanded_range_is_not_repeated_when_an_older_range_ends_before_it(self) -> None:
        repeated_range = hit(
            "Флп. 3:15-16", 86.0,
            matched=("впрочем", "достигнуть", "должный", "мыслить", "правило", "жить"),
            bigram=70.0,
            trigram=55.0,
            book_id=50,
            chapter=3,
            verse=15,
            end_verse=16,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[repeated_range], [repeated_range]]),
            self.config(immediate_score=99.0),
        )
        detector.mark_shown("Филиппийцам 3:13-14", now=0.0)
        detector.mark_shown("Филиппийцам 3:15-16", now=1.0)

        decision = detector.process_fragment(
            "впрочем достигли должны мыслить потому правилу жить", now=2.0,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("duplicate_cooldown", decision.reason)


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
    def test_subtitle_markers_cover_psalm_asr_variants_without_plain_salo(self) -> None:
        from tools.replay_audio_files import ADDRESS_MARKER_RE

        self.assertIsNotNone(ADDRESS_MARKER_RE.search("Откроем псалом девяностый."))
        self.assertIsNotNone(ADDRESS_MARKER_RE.search("пса лом двадцать второй"))
        self.assertIsNotNone(ADDRESS_MARKER_RE.search("сало двадцать второй"))
        self.assertIsNone(ADDRESS_MARKER_RE.search("после обеда было сало и хлеб"))

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

    def test_replay_inventory_finds_video_id_in_parent_directory(self) -> None:
        from tools.replay_audio_files import (
            collect_audio_youtube_sources,
            collect_subtitle_youtube_ids,
        )

        video_id = "67IR5lBlUqs"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio_dir = root / "audio" / video_id
            subtitle_dir = root / "subtitles" / video_id
            audio_dir.mkdir(parents=True)
            subtitle_dir.mkdir(parents=True)
            (audio_dir / "Воскресное богослужение 21 07 24_16k_mono.wav").touch()
            (subtitle_dir / "transcript.vtt").write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\nТекст\n",
                encoding="utf-8",
            )

            audio = collect_audio_youtube_sources([root / "audio"], root / "download", False)
            subtitles = collect_subtitle_youtube_ids([root / "subtitles"])

        self.assertIn(video_id, audio)
        self.assertIn(video_id, subtitles)
        self.assertNotIn("24_16k_mono", audio)

    def test_subtitle_window_plan_uses_timed_cues_and_merges_nearby_markers(self) -> None:
        from tools.replay_audio_files import write_subtitle_window_plan

        video_id = "67IR5lBlUqs"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio" / video_id / "sermon.wav"
            subtitle = root / "subtitles" / video_id / "sermon.srt"
            audio.parent.mkdir(parents=True)
            subtitle.parent.mkdir(parents=True)
            audio.touch()
            subtitle.write_text(
                "1\n00:01:00,000 --> 00:01:03,000\nОткроем вторую главу.\n\n"
                "2\n00:01:20,000 --> 00:01:23,000\nПрочитаем пятый стих.\n",
                encoding="utf-8",
            )
            plans = write_subtitle_window_plan([audio], [root / "subtitles"], root / "plans")
            plan = json.loads(plans[0].read_text(encoding="utf-8"))

        self.assertEqual(video_id, plan["video_id"])
        self.assertEqual("explicit_address_markers_and_bible_text_similarity", plan["selection"])
        self.assertEqual(1, len(plan["windows"]))
        self.assertEqual(15.0, plan["windows"][0]["start_seconds"])
        self.assertEqual(128.0, plan["windows"][0]["end_seconds"])
        self.assertEqual(["главу", "откроем", "прочитаем", "стих"], plan["windows"][0]["markers"])

    def test_subtitle_text_candidates_keep_only_strong_bible_matches(self) -> None:
        from tools.replay_audio_files import subtitle_text_candidates

        candidate = hit(
            "Ин. 3:16", 92.0,
            matched=("так", "возлюбить", "бог", "мир"),
            bigram=80.0,
            trigram=70.0,
        )
        searcher = FakeSearcher([[candidate]])
        cues = [
            {"start_seconds": 10.0, "end_seconds": 12.0, "text": "Ибо так"},
            {"start_seconds": 12.0, "end_seconds": 14.0, "text": "возлюбил Бог"},
            {"start_seconds": 14.0, "end_seconds": 16.0, "text": "мир"},
        ]

        candidates = subtitle_text_candidates(cues, searcher)

        self.assertEqual(1, len(candidates))
        self.assertEqual("bible_text_similarity", candidates[0]["sources"][0])
        self.assertEqual("Ин. 3:16", candidates[0]["matches"][0]["reference"])

    def test_subtitle_controls_are_ordinary_speech_outside_citation_windows(self) -> None:
        from tools.replay_audio_files import subtitle_control_candidates

        cues = [
            {"start_seconds": 20.0, "end_seconds": 25.0, "text": "Обычная речь до цитаты сегодня"},
            {"start_seconds": 75.0, "end_seconds": 80.0, "text": "Это не должно попасть в контроль"},
            {"start_seconds": 180.0, "end_seconds": 185.0, "text": "Обычная речь после цитаты сегодня"},
            {"start_seconds": 280.0, "end_seconds": 285.0, "text": "Завершающая обычная речь проповеди"},
            {"start_seconds": 300.0, "end_seconds": 305.0, "text": "Откроем следующую главу пожалуйста"},
        ]
        citation_windows = [{"start_seconds": 60.0, "end_seconds": 150.0}]

        controls = subtitle_control_candidates(cues, citation_windows, limit=2)

        self.assertEqual(2, len(controls))
        self.assertTrue(all(item["sources"] == ["plain_speech_control"] for item in controls))
        self.assertTrue(all(
            item["end_seconds"] <= 60.0 or item["start_seconds"] >= 150.0
            for item in controls
        ))
        self.assertNotIn("Откроем", " ".join(item["cue_text"] for item in controls))

    def test_window_audio_jobs_preserve_plan_origin_and_control_kind(self) -> None:
        from tools.replay_audio_files import window_audio_jobs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            source.touch()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "video_id": "67IR5lBlUqs",
                "audio": str(source),
                "windows": [
                    {"start_seconds": 10, "end_seconds": 55, "sources": ["plain_speech_control"]},
                    {"start_seconds": 60, "end_seconds": 110, "sources": ["explicit_address_marker"]},
                ],
            }), encoding="utf-8")

            jobs = window_audio_jobs([plan_path], root / "windows")

        self.assertEqual(2, len(jobs))
        self.assertEqual(45.0, jobs[0]["duration_seconds"])
        self.assertIn("01_control_part01_000010_000055.wav", jobs[0]["output_audio"])
        self.assertIn("02_citation_part01_000060_000110.wav", jobs[1]["output_audio"])

    def test_window_audio_jobs_split_long_windows_with_small_overlap(self) -> None:
        from tools.replay_audio_files import window_audio_jobs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            source.touch()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "video_id": "67IR5lBlUqs",
                "audio": str(source),
                "windows": [{"start_seconds": 0, "end_seconds": 250, "sources": ["explicit_address_marker"]}],
            }), encoding="utf-8")

            jobs = window_audio_jobs([plan_path], root / "windows")

        self.assertEqual([(0.0, 120.0), (115.0, 235.0), (230.0, 250.0)], [
            (job["start_seconds"], job["end_seconds"]) for job in jobs
        ])

    def test_replay_window_parts_keep_only_semantic_context(self) -> None:
        from bible_parser_core.live_pipeline import LiveReferencePipeline
        from tools.replay_audio_files import (
            restore_replay_session_context,
            save_replay_session_context,
        )

        first = LiveReferencePipeline()
        self.assertTrue(first.set_context_range({
            "book": "Бытие", "chapter": 22, "start_verse": 1,
            "end_chapter": 22, "end_verse": 19,
        }))
        state: dict[str, object] = {}
        save_replay_session_context(first, {"long_passage": {"ref": "Бытие 22:1-19"}}, state)

        following = LiveReferencePipeline()
        following.text_buffer.add("старый буфер не переносится")
        replay_state: dict[str, dict | None] = {"long_passage": None}
        self.assertTrue(restore_replay_session_context(following, replay_state, state))

        self.assertEqual("Бытие", following.context_range["book"])
        self.assertEqual(22, following.context_current_chapter)
        self.assertEqual({"ref": "Бытие 22:1-19"}, replay_state["long_passage"])
        self.assertEqual(["старый буфер не переносится"], list(following.text_buffer.parts))

    def test_excluded_cascade_is_recorded_but_not_exported_for_training(self) -> None:
        from tools.analyze_vosk_probe_logs import export_training_data

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "20260907_130000_000001"
            run.mkdir()
            (run / "session.json").write_text('{"asr_engine": "sherpa-0.54"}\n', encoding="utf-8")
            (run / "trigger_cases.jsonl").write_text(
                json.dumps({"case_id": "trigger_0001", "ref": "Притчи 1:1", "status": "reviewed", "review_category": "excluded_cascade"}) + "\n"
                + json.dumps({"case_id": "trigger_0002", "ref": "Иоанн 3:16", "status": "reviewed", "review_category": "true_reference"}) + "\n",
                encoding="utf-8",
            )
            output = root / "training.csv"
            report = export_training_data(root, output, asr_engine="sherpa-0.54")
            csv_text = output.read_text(encoding="utf-8")

        self.assertEqual(1, report["rows"])
        self.assertEqual(1, report["excluded"])
        self.assertNotIn("excluded_cascade", csv_text)
        self.assertIn("trigger_0002", csv_text)

    def test_replay_batch_summary_lists_source_file_citations_and_timecodes(self) -> None:
        from tools.replay_audio_files import replay_batch_summary_lines, write_replay_batch_summary

        with tempfile.TemporaryDirectory() as temporary:
            log_dir = Path(temporary)
            first_run = log_dir / "20260903_120000_000001"
            second_run = log_dir / "20260903_120100_000001"
            first_run.mkdir()
            second_run.mkdir()
            (first_run / "session.json").write_text(
                '{"source_audio": "C:/audio/first_sermon.wav"}\n', encoding="utf-8"
            )
            (second_run / "session.json").write_text(
                '{"source_audio": "C:/audio/second_sermon.wav"}\n', encoding="utf-8"
            )
            (first_run / "trigger_cases.jsonl").write_text(
                '{"timecode":"00:01:02.000","ref":"Иоанн 3:16",'
                '"payload":{"source":"parser"}}\n',
                encoding="utf-8",
            )

            lines = replay_batch_summary_lines([first_run, second_run])
            summary_path = write_replay_batch_summary(log_dir, [first_run, second_run])

            self.assertIn("Файл: first_sermon.wav", lines)
            self.assertIn("  1. 00:01:02.000  Иоанн 3:16 — по адресу", lines)
            self.assertIn("Файл: second_sermon.wav", lines)
            self.assertIn("  Цитаты не обнаружены.", lines)
            self.assertIsNotNone(summary_path)
            self.assertEqual("\n".join(lines) + "\n", summary_path.read_text(encoding="utf-8"))

    def test_rodnik_annotation_history_includes_previous_batches(self) -> None:
        from tools.replay_audio_files import annotation_history_case_files, annotation_stats

        with tempfile.TemporaryDirectory() as temporary:
            history_root = Path(temporary) / "rodnik_replay_batches"
            first = history_root / "20260907_120000" / "logs" / "first"
            current = history_root / "20260908_120000" / "logs" / "current"
            first.mkdir(parents=True)
            current.mkdir(parents=True)
            (first / "trigger_cases.jsonl").write_text(
                json.dumps({"case_id": "trigger_0001", "status": "reviewed", "ref": "Иоанн 3:16"}) + "\n",
                encoding="utf-8",
            )
            (current / "trigger_cases.jsonl").write_text(
                json.dumps({"case_id": "trigger_0001", "status": "unreviewed", "ref": "Ефесянам 3:14"}) + "\n",
                encoding="utf-8",
            )

            paths, label = annotation_history_case_files(current.parent)
            stats = annotation_stats(paths)

        self.assertEqual("Во всех пачках Родника", label)
        self.assertEqual(2, stats["files"])
        self.assertEqual(1, stats["reviewed"])
        self.assertEqual(1, stats["unreviewed"])

    def test_replay_overlap_duplicate_is_excluded_from_annotation(self) -> None:
        from tools.replay_audio_files import exclude_replay_overlap_duplicates, load_jsonl

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_run = root / "first"
            second_run = root / "second"
            first_run.mkdir()
            second_run.mkdir()
            for run, source_name, first_word in (
                (first_run, "08_citation_part01_001286_001406.wav", 115.39),
                (second_run, "08_citation_part02_001401_001469.wav", 0.0),
            ):
                source = root / "video" / source_name
                (run / "session.json").write_text(
                    json.dumps({"source_audio": str(source)}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (run / "trigger_cases.jsonl").write_text(
                    json.dumps({
                        "case_id": "trigger_0001",
                        "status": "unreviewed",
                        "ref": "Марк 12:29-30",
                        "asr": {"result": [{"start": first_word}]},
                    }, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

            self.assertEqual(1, exclude_replay_overlap_duplicates([first_run, second_run]))
            self.assertEqual("unreviewed", load_jsonl(first_run / "trigger_cases.jsonl")[0]["status"])
            duplicate = load_jsonl(second_run / "trigger_cases.jsonl")[0]
            self.assertEqual("reviewed", duplicate["status"])
            self.assertEqual("excluded_cascade", duplicate["review_category"])

    def test_sherpa_subwords_are_converted_to_timed_vosk_words(self) -> None:
        from bible_parser_core.sherpa_streaming import DEFAULT_SHERPA_THREADS
        from tools.replay_audio_files import sherpa_result_to_vosk_result

        self.assertEqual(1, DEFAULT_SHERPA_THREADS)

        result = SimpleNamespace(
            text="иаков четвёртая глава",
            tokens=[" и", "а", "ко", "в", " че", "т", "вёр", "та", "я", " глава"],
            timestamps=[0.4, 0.5, 0.6, 0.7, 1.0, 1.1, 1.2, 1.3, 1.4, 1.8],
            ys_probs=[-0.1] * 10,
        )

        converted = sherpa_result_to_vosk_result(result, time_offset=10.0)

        self.assertEqual(converted["text"], "иаков четвёртая глава")
        self.assertEqual(
            [item["word"] for item in converted["result"]],
            ["иаков", "четвёртая", "глава"],
        )
        self.assertEqual(converted["result"][0]["start"], 10.4)
        self.assertEqual(converted["result"][0]["end"], 11.0)
        self.assertGreater(converted["result"][0]["conf"], 0.9)

    def test_replay_summary_groups_overlapping_windows_into_one_citation_event(self) -> None:
        from tools.replay_audio_files import citation_event_summary_lines

        cases = [
            {
                "timecode": "00:26:52.000",
                "timecode_seconds": 1612.0,
                "ref": "Иаков 1:22-23",
                "payload": {
                    "book": "Иаков", "chapter": 1,
                    "start_verse": 22, "end_verse": 23,
                    "source": "text_citation",
                },
            },
            {
                "timecode": "00:27:03.000",
                "timecode_seconds": 1623.0,
                "ref": "Иаков 1:23-24",
                "payload": {
                    "book": "Иаков", "chapter": 1,
                    "start_verse": 23, "end_verse": 24,
                    "source": "text_citation",
                },
            },
            {
                "timecode": "00:34:15.000",
                "timecode_seconds": 2055.0,
                "ref": "Матфей 7:21",
                "payload": {
                    "book": "Матфей", "chapter": 7,
                    "start_verse": 21, "end_verse": 21,
                    "source": "text_citation",
                },
            },
            {
                "timecode": "00:34:19.000",
                "timecode_seconds": 2059.0,
                "ref": "Матфей 7:21",
                "payload": {
                    "book": "Матфей", "chapter": 7,
                    "start_verse": 21, "end_verse": 21,
                    "source": "parser",
                },
            },
        ]

        lines = citation_event_summary_lines(cases)

        self.assertEqual(len(lines), 2)
        self.assertIn("Иаков 1:22-24 — по тексту; объединено окон: 2", lines[0])
        self.assertIn("Матфей 7:21 — по тексту + по адресу; объединено окон: 2", lines[1])

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
