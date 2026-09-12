from __future__ import annotations

import unittest
import tempfile
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from bible_parser_core.bible_text_search import BibleTextSearchResult
from bible_parser_core.parser import DEFAULT_BIBLE
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
    ending_overlap_words: int = 0,
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
        ending_overlap_words=ending_overlap_words,
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

    def test_incomplete_address_can_be_corrected_by_rich_verse_text(self) -> None:
        misleading = hit(
            "4Цар. 7:2",
            69.52,
            matched=("един", "есть", "люди"),
            ordered=60.0,
            bigram=20.0,
            trigram=0.0,
            book_id=12,
            chapter=7,
            verse=2,
        )
        misleading_second = hit(
            "Ин. 1:9",
            64.25,
            matched=("есть", "люди"),
            ordered=50.0,
            bigram=0.0,
            trigram=0.0,
        )
        deuteronomy = hit(
            "Втор. 6:4",
            60.36,
            matched=("слушать", "израиль", "господь", "господь", "бог", "наш", "единый", "есть"),
            ordered=70.83,
            bigram=50.0,
            trigram=46.15,
            book_id=5,
            chapter=6,
            verse=4,
        )
        mark = hit(
            "Мк. 12:29",
            50.94,
            matched=("слушать", "израиль", "господь", "бог"),
            ordered=59.14,
            bigram=35.71,
            trigram=23.08,
            book_id=41,
            chapter=12,
            verse=29,
        )
        detector = ScriptureTextDetector(
            FakeSearcher(
                [
                    [misleading, misleading_second],
                    [deuteronomy, mark],
                ]
            ),
            self.config(window_sizes=(5, 14)),
        )

        corrected = detector.process_fragment(
            "лишнего ничего нет слушай израиль господь бог наш господь един есть но если люди",
            now=1.0,
            incomplete_address_correction=True,
        )

        self.assertTrue(corrected.accepted)
        self.assertEqual("Втор. 6:4", corrected.reference)
        self.assertEqual("text_corrected_incomplete_address", corrected.reason)

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

    def test_ambiguous_broader_range_keeps_well_matched_verse_before_strong_suffix(self) -> None:
        suffix = hit(
            "Быт. 2:17", 92.609,
            matched=("который", "вкусить", "от", "он", "смерть", "умереть"),
            bigram=70.0,
            trigram=55.0,
            book_id=1,
            chapter=2,
            verse=17,
        )
        full_range = hit(
            "Быт. 2:16-17", 85.516,
            matched=(
                "человек", "всякий", "дерево", "сад", "есть", "дерево", "познание",
                "добро", "зло", "есть", "вкусить", "смерть", "умереть",
            ),
            bigram=65.0,
            trigram=50.0,
            book_id=1,
            chapter=2,
            verse=16,
            end_verse=17,
        )
        competing = hit(
            "Исх. 1:1", 82.268, matched=("дерево", "есть"), bigram=10.0, trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[suffix], [full_range, competing]]),
            self.config(window_sizes=(5, 20), buffer_words=20, immediate_score=90.0),
        )

        decision = detector.process_fragment(
            "человек всякий дерева сад есть дерево познание добро зло не есть вкусить смерть умереть",
            now=0.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Быт. 2:16-17", decision.reference)
        self.assertEqual("broader_range_with_strong_suffix", decision.reason)

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

    def test_high_risk_address_allows_text_to_widen_displayed_range(self) -> None:
        widened = hit(
            "Иер. 32:40-42", 96.0,
            matched=("заключить", "завет", "вечный", "страх", "сердце"),
            trigram=75.0,
            book_id=24,
            chapter=32,
            verse=40,
            end_verse=42,
        )
        detector = ScriptureTextDetector(
            FakeSearcher([[widened]] * 8),
            self.config(immediate_score=90.0),
        )

        # This is the high-risk path: the address slide is remembered only to
        # prevent a duplicate 32:42, not to suppress the text search.
        detector.mark_shown("Иеремия 32:42", now=2.0)
        decision = detector.process_fragment(
            "заключу с ними завет вечный страх мой вложу в сердца их",
            now=2.1,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Иер. 32:40-42", decision.reference)

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

    def test_noisy_next_range_with_many_matches_is_accepted_after_shown_range(self) -> None:
        following = hit(
            "Ис. 40:5-6", 63.012,
            matched=(
                "явиться", "слава", "господень", "узреть", "всякий", "плоть",
                "спасение", "божий", "уста", "изречь",
            ),
            bigram=30.0,
            trigram=20.0,
            book_id=23,
            chapter=40,
            verse=5,
            end_verse=6,
        )
        competing = hit("Иер. 1:1", 48.553, matched=("господень", "всякий"), bigram=0.0, trigram=0.0)
        detector = ScriptureTextDetector(
            FakeSearcher([[following, competing]]),
            self.config(immediate_score=99.0, window_sizes=(11,)),
        )
        detector.mark_shown("Исаия 40:3-4", now=0.0)

        decision = detector.process_fragment(
            "явиться слава господень узреть всякий плоть спасение божий уста изречь голос", now=1.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("Ис. 40:5-6", decision.reference)
        self.assertEqual("relaxed_continuation_after_shown_range", decision.reason)

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

    def test_replay_inventory_finds_ytdlp_language_suffixed_subtitle(self) -> None:
        from tools.replay_audio_files import youtube_ids_from_path

        self.assertEqual(
            ["AbCdEf12345"],
            youtube_ids_from_path(Path("AbCdEf12345.ru.vtt")),
        )

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
                "1\n00:00:10,000 --> 00:00:15,000\nОбычная речь перед чтением сегодня.\n\n"
                "2\n00:01:00,000 --> 00:01:03,000\nОткроем вторую главу.\n\n"
                "3\n00:01:20,000 --> 00:01:23,000\nПрочитаем пятый стих.\n",
                encoding="utf-8",
            )
            plans = write_subtitle_window_plan(
                [audio],
                [root / "subtitles"],
                root / "plans",
                control_windows=0,
            )
            plan = json.loads(plans[0].read_text(encoding="utf-8"))
            audit_plans = write_subtitle_window_plan(
                [audio],
                [root / "subtitles"],
                root / "audit-plans",
                control_windows=1,
                control_only=True,
            )
            audit_plan = json.loads(audit_plans[0].read_text(encoding="utf-8"))

        self.assertEqual(video_id, plan["video_id"])
        self.assertEqual("structured_address_markers_and_bible_text_similarity", plan["selection"])
        self.assertEqual(1, len(plan["windows"]))
        self.assertEqual(45.0, plan["windows"][0]["start_seconds"])
        self.assertEqual(128.0, plan["windows"][0]["end_seconds"])
        self.assertEqual(["главу", "откроем", "прочитаем", "стих"], plan["windows"][0]["markers"])
        self.assertEqual("plain_speech_control", audit_plan["selection"])
        self.assertEqual(1, len(audit_plan["windows"]))
        self.assertEqual(["plain_speech_control"], audit_plan["windows"][0]["sources"])

    def test_subtitle_marker_candidates_reject_unstructured_marker_words(self) -> None:
        from tools.replay_audio_files import subtitle_marker_candidates

        cues = [
            {
                "start_seconds": 10.0,
                "end_seconds": 13.0,
                "text": "Моё послание для всех, кому откроют двери",
            },
        ]

        self.assertEqual([], subtitle_marker_candidates(cues))

    def test_subtitle_marker_candidates_keep_split_chapter_and_verse_numbers(self) -> None:
        from tools.replay_audio_files import merge_subtitle_window_candidates, subtitle_marker_candidates

        cues = [
            {"start_seconds": 60.0, "end_seconds": 63.0, "text": "Откроем вторую главу."},
            {"start_seconds": 80.0, "end_seconds": 83.0, "text": "Прочитаем пятый стих."},
        ]

        windows = merge_subtitle_window_candidates(subtitle_marker_candidates(cues))

        self.assertEqual(1, len(windows))
        self.assertEqual(45.0, windows[0]["start_seconds"])
        self.assertEqual(128.0, windows[0]["end_seconds"])

    def test_long_announced_range_keeps_audio_through_its_last_verses(self) -> None:
        from tools.replay_audio_files import merge_subtitle_window_candidates, subtitle_marker_candidates

        candidates = subtitle_marker_candidates([
            {
                "start_seconds": 2372.91,
                "end_seconds": 2393.2,
                "text": "послание колосянам первая глава с девятого по четырнадцатый стих",
            },
        ])
        windows = merge_subtitle_window_candidates(candidates)

        self.assertEqual(["Колоссянам 1:9-14"], candidates[0]["references"])
        self.assertEqual(6, candidates[0]["range_verse_count"])
        self.assertEqual(81.0, candidates[0]["padding_after_seconds"])
        self.assertEqual(2474.2, windows[0]["end_seconds"])

    def test_subtitle_windows_do_not_merge_only_because_padding_overlaps(self) -> None:
        from tools.replay_audio_files import merge_subtitle_window_candidates, subtitle_marker_candidates

        cues = [
            {"start_seconds": 60.0, "end_seconds": 63.0, "text": "Откроем вторую главу."},
            {"start_seconds": 100.0, "end_seconds": 103.0, "text": "Прочитаем пятый стих."},
        ]

        windows = merge_subtitle_window_candidates(subtitle_marker_candidates(cues))

        self.assertEqual(2, len(windows))

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

    def test_window_audio_jobs_allow_plan_without_selected_windows(self) -> None:
        from tools.replay_audio_files import window_audio_jobs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            source.touch()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "video_id": "67IR5lBlUqs",
                "audio": str(source),
                "windows": [],
            }), encoding="utf-8")

            jobs = window_audio_jobs([plan_path], root / "windows")

        self.assertEqual([], jobs)

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

    def test_window_audio_jobs_do_not_replay_overlap_of_neighbouring_windows(self) -> None:
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
                    {"start_seconds": 2396.2, "end_seconds": 2497.28,
                     "sources": ["explicit_address_marker"]},
                    {"start_seconds": 2487.119, "end_seconds": 2579.88,
                     "sources": ["bible_text_similarity"]},
                ],
            }), encoding="utf-8")

            jobs = window_audio_jobs([plan_path], root / "windows")

        self.assertEqual([(2396.2, 2497.28), (2497.28, 2579.88)], [
            (job["start_seconds"], job["end_seconds"]) for job in jobs
        ])
        self.assertEqual(2487.119, jobs[1]["planned_start_seconds"])

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
        save_replay_session_context(first, {
            "long_passage": {"ref": "Бытие 22:1-19"},
            "smart_slide_shadow": {
                "ref": "Бытие 22:1-19", "current_index": 3, "targets": [{"verse": 4}],
            },
        }, state)

        following = LiveReferencePipeline()
        following.text_buffer.add("старый буфер не переносится")
        replay_state: dict[str, dict | None] = {
            "long_passage": None, "smart_slide_shadow": None,
        }
        self.assertTrue(restore_replay_session_context(following, replay_state, state))

        self.assertEqual("Бытие", following.context_range["book"])
        self.assertEqual(22, following.context_current_chapter)
        self.assertEqual({"ref": "Бытие 22:1-19"}, replay_state["long_passage"])
        self.assertEqual(3, replay_state["smart_slide_shadow"]["current_index"])
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

    def test_training_export_includes_nested_rodnik_replay_runs(self) -> None:
        from tools.analyze_vosk_probe_logs import export_training_data

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "20260909_072205" / "logs" / "20260909_072750_688325"
            run.mkdir(parents=True)
            (run / "session.json").write_text('{"asr_engine": "sherpa-0.54"}\n', encoding="utf-8")
            (run / "trigger_cases.jsonl").write_text(
                json.dumps({
                    "case_id": "trigger_0002", "ref": "Иеремия 32:42",
                    "status": "reviewed", "review_category": "vosk_distortion",
                }) + "\n",
                encoding="utf-8",
            )
            output = root / "training.csv"
            report = export_training_data(root, output, asr_engine="sherpa-0.54")

        self.assertEqual(1, report["rows"])
        self.assertEqual(1, report["target_confirm"])

    def test_smart_slide_training_export_keeps_reviews_separate_from_reference_risk(self) -> None:
        from tools.analyze_vosk_probe_logs import (
            export_smart_slide_training_data,
            train_smart_slide_model,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "20260911_130000_000001"
            run.mkdir()
            (run / "session.json").write_text(
                '{"asr_engine":"sherpa-0.54","mode":"audio_replay"}\n', encoding="utf-8"
            )
            events = [
                {
                    "event": "SMART_SLIDE_SHADOW", "passage": "Марк 1:21-24",
                    "slide_mode": "one_verse", "action": "advance", "reason": "strong_current_boundary",
                    "evidence_source": "sequence_scoped", "score": 95, "margin": 80,
                    "matched_words": 5, "current_index": 0, "candidate_index": 0, "target_index": 1,
                    "current_element": {"verse": 21}, "candidate_element": {"verse": 21},
                    "target_element": {"verse": 22}, "window": "конец двадцать первого стиха", "replay_seconds": 12,
                },
                {
                    "event": "SMART_SLIDE_SHADOW", "passage": "Марк 1:21-24",
                    "slide_mode": "one_verse", "action": "assisted_advance", "reason": "assisted_current_boundary",
                    "evidence_source": "sequence_scoped", "score": 62, "margin": 14,
                    "matched_words": 2, "current_index": 1, "candidate_index": 1, "target_index": 2,
                    "current_element": {"verse": 22}, "candidate_element": {"verse": 22},
                    "target_element": {"verse": 23}, "window": "случайные слова", "replay_seconds": 22,
                },
                {
                    "event": "SMART_SLIDE_SHADOW", "passage": "Марк 1:21-24",
                    "slide_mode": "one_verse", "action": "keep", "reason": "current_end_not_heard",
                    "evidence_source": "sequence_scoped", "score": 70, "margin": 40,
                    "matched_words": 4, "current_index": 2, "candidate_index": 2,
                    "current_element": {"verse": 23}, "candidate_element": {"verse": 23},
                    "window": "стих еще читается", "replay_seconds": 28,
                },
            ]
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
            )
            reviews = [
                {"event_id": f"{run.name}:smart_slide:1", "review_category": "correct_transition"},
                {"event_id": f"{run.name}:smart_slide:2", "review_category": "wrong_transition"},
                {"event_id": f"{run.name}:smart_slide:3", "review_category": "correct_hold"},
            ]
            (run / "smart_slide_reviews.jsonl").write_text(
                "".join(json.dumps(review, ensure_ascii=False) + "\n" for review in reviews), encoding="utf-8"
            )
            csv_path = root / "smart_slide_training.csv"
            export = export_smart_slide_training_data(root, csv_path, asr_engine="sherpa-0.54")
            model_path = root / "smart_slide_model.json"
            report_path = root / "smart_slide_model_report.json"
            trained = train_smart_slide_model(csv_path, model_path, report_path)
            with csv_path.open(encoding="utf-8", newline="") as stream:
                rows = list(__import__("csv").DictReader(stream))
            model = json.loads(model_path.read_text(encoding="utf-8"))

        self.assertEqual(3, export["rows"])
        self.assertEqual(2, export["eligible_rows"])
        self.assertEqual(1, export["target_confirm"])
        self.assertEqual(["0", "1", ""], [row["target_confirm"] for row in rows])
        self.assertEqual(["1", "1", "0"], [row["training_eligible"] for row in rows])
        self.assertEqual(2, trained["rows"])
        self.assertEqual("smart_slide_transition_safety", model["scope"])

    def test_future_smart_slide_decision_is_not_training_label_after_early_stop(self) -> None:
        from tools.analyze_vosk_probe_logs import smart_slide_training_row

        entry = SimpleNamespace(
            event={"action": "assisted_advance", "replay_seconds": 50.25},
            review={"review_category": "wrong_transition", "operator_stop_seconds": 24.422},
            events_path=Path("/tmp/nonexistent/events.jsonl"),
            event_id="example:smart_slide:1",
            sequence_id=0,
            sequence_position=1,
            sequence_total=1,
        )

        row = smart_slide_training_row(entry)

        self.assertEqual(0, row["training_eligible"])
        self.assertEqual("", row["target_confirm"])

    def test_visual_smart_slide_review_saves_same_annotation_format(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "20260911_140000_000001"
            run.mkdir()
            (run / "events.jsonl").write_text(json.dumps({
                "event": "SMART_SLIDE_SHADOW", "passage": "Матфей 28:5-10",
                "action": "advance", "reason": "strong_current_boundary", "score": 93,
                "margin": 70, "matched_words": 5, "current_index": 0, "target_index": 1,
                "replay_seconds": 18.5, "current_element": {"verse": 5, "text": "Ангел же"},
                "target_element": {"verse": 6, "text": "Его нет здесь"},
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            entries = collect_smart_slide_entries([run / "events.jsonl"])
            controller = SmartSlideBrowserReview(entries, no_resume=False)
            state = controller.state()
            next_state = controller.save_review("wrong_transition", "перешёл раньше чтения")
            review = json.loads((run / "smart_slide_reviews.jsonl").read_text(encoding="utf-8"))

        self.assertEqual("Матфей 28:5-10", state["passage"])
        self.assertTrue(state["will_transition"])
        self.assertTrue(state["will_display"])
        self.assertEqual("Его нет здесь", state["target"]["text"])
        self.assertEqual("wrong_transition", review["review_category"])
        self.assertEqual("перешёл раньше чтения", review["note"])
        self.assertEqual(0, next_state["remaining"])

    def test_visual_smart_slide_review_marks_initial_activation_as_display(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "20260912_101129_085386"
            run.mkdir()
            (run / "events.jsonl").write_text(json.dumps({
                "event": "SMART_SLIDE_SHADOW", "passage": "Колоссянам 1:9-14",
                "action": "activate", "reason": "assisted_initial_element", "score": 67.558,
                "margin": 57.659, "matched_words": 3, "current_index": 0,
                "replay_seconds": 52.0,
                "current_element": {"verse": 9, "text": "Посему и мы"},
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            entry = collect_smart_slide_entries([run / "events.jsonl"])[0]
            state = SmartSlideBrowserReview([entry], no_resume=False).state()

        self.assertEqual("показать первый стих диапазона", state["action_label"])
        self.assertFalse(state["will_transition"])
        self.assertTrue(state["will_display"])
        self.assertEqual("Посему и мы", state["current"]["text"])

    def test_continuous_smart_slide_review_marks_only_observed_decisions_correct(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries, load_jsonl

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "20260911_141000_000001"
            run.mkdir()
            events = [
                {"event": "SMART_SLIDE_SHADOW", "passage": "Матфей 28:5-10", "action": "advance",
                 "current_index": 0, "target_index": 1, "replay_seconds": 10},
                {"event": "SMART_SLIDE_SHADOW", "passage": "Матфей 28:5-10", "action": "keep",
                 "current_index": 1, "replay_seconds": 15},
                {"event": "SMART_SLIDE_SHADOW", "passage": "Матфей 28:5-10", "action": "advance",
                 "current_index": 1, "target_index": 2, "replay_seconds": 20},
            ]
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
            )
            entries = collect_smart_slide_entries([run / "events.jsonl"])
            controller = SmartSlideBrowserReview(entries, no_resume=False)
            timeline = controller.timeline()
            controller.mark_error(entries[0].event_id, "wrong_transition", "слишком рано", 12.4)
            result = controller.complete_sequence([entries[0].event_id, entries[1].event_id])
            reviews = {row["event_id"]: row for row in load_jsonl(run / "smart_slide_reviews.jsonl")}

        self.assertEqual(3, len(timeline["tracks"][0]["decisions"]))
        self.assertEqual("wrong_transition", reviews[entries[0].event_id]["review_category"])
        self.assertEqual(12.4, reviews[entries[0].event_id]["operator_stop_seconds"])
        self.assertEqual("correct_hold", reviews[entries[1].event_id]["review_category"])
        self.assertNotIn(entries[2].event_id, reviews)
        self.assertEqual(1, result["remaining"])

    def test_browser_timeline_includes_normal_liverse_slide_updates(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "20260911_142000_000001"
            run.mkdir()
            events = [
                {"event": "SMART_SLIDE_SHADOW", "passage": "Иаков 2:15-20", "action": "keep",
                 "current_index": 5, "replay_seconds": 68, "current_element": {"verse": 20}},
                {"event": "final_raw", "text": "евангелие от матфея седьмая глава", "replay_seconds": 72.5},
                {"event": "parsed", "vosk_text": "евангелие от матфея седьмая глава двадцать шестой",
                 "payload": {"ref": "Матфей 7:26-27", "source": "parser"},
                 "output": {"replay": {"sent": True}}},
            ]
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
            )
            entries = collect_smart_slide_entries([run / "events.jsonl"])
            timeline = SmartSlideBrowserReview(entries, no_resume=False).timeline()

        update = timeline["tracks"][0]["display_updates"][0]
        self.assertEqual("Матфей 7:26-27", update["ref"])
        self.assertEqual(72.5, update["replay_seconds"])
        self.assertIn("всякий", update["element"]["text"].lower())

    def test_browser_timeline_shows_only_address_when_long_range_is_announced(self) -> None:
        from tools.review_trigger_cases import normal_slide_updates

        with tempfile.TemporaryDirectory() as temporary:
            events_path = Path(temporary) / "events.jsonl"
            events_path.write_text("\n".join([
                json.dumps({"event": "final_raw", "replay_seconds": 12.5}, ensure_ascii=False),
                json.dumps({
                    "event": "parsed", "payload": {"ref": "Иаков 1:5-10", "source": "parser"},
                    "output": {"replay": {"sent": True}},
                }, ensure_ascii=False),
            ]) + "\n", encoding="utf-8")
            updates = normal_slide_updates(events_path)

        self.assertEqual("range_announcement", updates[0]["kind"])
        self.assertEqual("Иаков 1:5-10", updates[0]["ref"])
        self.assertEqual("", updates[0]["element"]["text"])

    def test_browser_timeline_shows_current_verse_for_inferred_sequential_reading(self) -> None:
        from tools.review_trigger_cases import normal_slide_updates

        with tempfile.TemporaryDirectory() as temporary:
            events_path = Path(temporary) / "events.jsonl"
            events_path.write_text("\n".join([
                json.dumps({"event": "final_raw", "replay_seconds": 90.5}, ensure_ascii=False),
                json.dumps({
                    "event": "parsed",
                    "payload": {
                        "ref": "Иоанн 14:1-3",
                        "source": "replay_inferred_sequential_text_reading",
                    },
                    "output": {"replay": {"sent": True}},
                }, ensure_ascii=False),
            ]) + "\n", encoding="utf-8")
            updates = normal_slide_updates(events_path)

        self.assertEqual("sequential_reading", updates[0]["kind"])
        self.assertEqual("Иоанн 14:1-3", updates[0]["ref"])
        self.assertEqual(3, updates[0]["element"]["verse"])
        self.assertIn("и когда пойду", updates[0]["element"]["text"].lower())

    def test_browser_stop_writes_separate_diagnostic_incident(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries, load_jsonl

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "20260911_143000_000001"
            run.mkdir()
            events = [
                {"event": "SMART_SLIDE_SHADOW", "passage": "Иаков 2:15-20", "action": "keep",
                 "current_index": 5, "replay_seconds": 68, "current_element": {"verse": 20}},
                {"event": "final_raw", "text": "евангелие от матфея", "replay_seconds": 72.5},
                {"event": "TEXT_ACCEPTED", "reference": "Мф. 7:26-27", "replay_seconds": 74},
            ]
            (run / "events.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
            )
            entries = collect_smart_slide_entries([run / "events.jsonl"])
            result = SmartSlideBrowserReview(entries, no_resume=False).record_incident(
                entries[0].event_id, 75.5, "Иаков 2:20", "здесь уже Матфей"
            )
            incidents = load_jsonl(run / "smart_slide_incidents.jsonl")

        self.assertEqual(1, len(incidents))
        self.assertEqual("Иаков 2:20", incidents[0]["displayed_ref"])
        self.assertEqual("Мф. 7:26-27", incidents[0]["evidence"][-1]["ref"])
        self.assertIn("log=", result["report"])
        self.assertIn("timecode=01:15.500", result["report"])

    def test_browser_session_error_report_contains_only_stops_from_that_session(self) -> None:
        from tools.review_trigger_cases import SmartSlideBrowserReview, collect_smart_slide_entries

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "20260911_143000_000001"
            run.mkdir()
            (run / "events.jsonl").write_text(
                json.dumps(
                    {"event": "SMART_SLIDE_SHADOW", "passage": "Иаков 2:15-20", "action": "keep",
                     "reason": "current_end_not_heard", "replay_seconds": 68, "current_element": {"verse": 20}},
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            entries = collect_smart_slide_entries([run / "events.jsonl"])
            controller = SmartSlideBrowserReview(entries, no_resume=False, report_dir=root)
            incident = controller.record_incident(entries[0].event_id, 75.5, "Иаков 2:20", "")
            controller.update_incident_note(incident["incident_id"], "здесь уже Матфей")
            report = controller.write_error_report().read_text(encoding="utf-8")

        self.assertIn("Зафиксировано остановок Enter: 1", report)
        self.assertIn("Иаков 2:20", report)
        self.assertIn("current_end_not_heard", report)
        self.assertIn("здесь уже Матфей", report)

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

    def test_replay_overlap_keeps_different_reading_lists(self) -> None:
        from tools.replay_audio_files import exclude_replay_overlap_duplicates, load_jsonl

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_run = root / "first"
            second_run = root / "second"
            first_run.mkdir()
            second_run.mkdir()
            for run, source_name, first_word, references in (
                (first_run, "08_citation_part01_001286_001406.wav", 115.39, ["Псалтирь 9:19", "Псалтирь 38:8"]),
                (second_run, "08_citation_part02_001401_001469.wav", 0.0, ["Псалтирь 39:5", "Псалтирь 61:5"]),
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
                        "ref": "Ссылки для чтения",
                        "payload": {"reference_list": [{"ref": ref} for ref in references]},
                        "asr": {"result": [{"start": first_word}]},
                    }, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

            self.assertEqual(0, exclude_replay_overlap_duplicates([first_run, second_run]))
            self.assertEqual("unreviewed", load_jsonl(first_run / "trigger_cases.jsonl")[0]["status"])
            self.assertEqual("unreviewed", load_jsonl(second_run / "trigger_cases.jsonl")[0]["status"])

    def test_reading_list_accumulates_across_adjacent_finals(self) -> None:
        from tools.vosk_grammar_probe import accumulate_reading_list, add_slide_payload

        accumulated: list[dict] = []
        first = add_slide_payload({
            "text": "псалом девятый девятнадцатый стих",
            "reference_list": [{"ref": "Псалтирь 9:19"}, {"ref": "Псалтирь 38:8"}],
        })
        second = add_slide_payload({
            "text": "псалом тридцать девять пятый стих",
            "reference_list": [{"ref": "Псалтирь 39:5"}, {"ref": "Псалтирь 61:5"}],
        })

        accumulate_reading_list(first, accumulated)
        accumulate_reading_list(second, accumulated)
        accumulate_reading_list({"reference_list": []}, accumulated)

        self.assertEqual(
            ["Псалтирь 9:19", "Псалтирь 38:8", "Псалтирь 39:5", "Псалтирь 61:5"],
            [item["ref"] for item in second["reference_list"]],
        )
        self.assertEqual("Псалтирь 9:19\nПсалтирь 38:8\nПсалтирь 39:5\nПсалтирь 61:5", second["slide"]["verse"])
        self.assertEqual([], accumulated)

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

    def test_sherpa_replay_exposes_partial_text_before_endpoint(self) -> None:
        from bible_parser_core.sherpa_streaming import SherpaReplayRecognizer

        adapter = SherpaReplayRecognizer.__new__(SherpaReplayRecognizer)
        adapter.stream = object()
        adapter.recognizer = SimpleNamespace(
            get_result_all=lambda stream: SimpleNamespace(text="вошел он в синагогу и учил")
        )

        self.assertEqual("вошел он в синагогу и учил", adapter.partial_result())

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

    def test_replay_expands_explicit_small_chapter_reading_announcement(self) -> None:
        from tools.replay_audio_files import infer_replay_chapter_reading

        inferred = infer_replay_chapter_reading({
            "text": "сегодня мы будем читать шестьдесят второй псалом",
            "source": "parser",
            "parsed": {
                "book": "Псалтирь", "chapter": 62,
                "start_verse": 1, "end_chapter": 62, "end_verse": 1,
                "ref": "Псалтирь 62:1",
            },
        })

        self.assertIsNotNone(inferred)
        self.assertEqual("Псалтирь 62:1-12", inferred["parsed"]["ref"])
        self.assertTrue(inferred["replay_inferred_chapter_reading"])

    def test_replay_does_not_expand_a_bare_chapter_reference(self) -> None:
        from tools.replay_audio_files import infer_replay_chapter_reading

        inferred = infer_replay_chapter_reading({
            "text": "шестьдесят второй псалом говорит о жажде по богу",
            "source": "parser",
            "parsed": {
                "book": "Псалтирь", "chapter": 62,
                "start_verse": 1, "end_chapter": 62, "end_verse": 1,
                "ref": "Псалтирь 62:1",
            },
        })

        self.assertIsNone(inferred)

    def test_replay_opens_and_grows_only_a_sequential_text_reading(self) -> None:
        from tools.replay_audio_files import (
            extend_open_ended_replay_passage,
            infer_replay_sequential_text_reading,
            replay_smart_slide_state,
        )

        state = {}
        first = {
            "source": "text_citation",
            "parsed": {
                "book": "Иоанн", "chapter": 14,
                "start_verse": 1, "end_chapter": 14, "end_verse": 2,
            },
        }
        second = {
            "source": "text_citation",
            "parsed": {
                "book": "Иоанн", "chapter": 14,
                "start_verse": 2, "end_chapter": 14, "end_verse": 3,
            },
        }
        self.assertIsNone(infer_replay_sequential_text_reading(first, state, 80.25))
        inferred = infer_replay_sequential_text_reading(second, state, 90.5)

        self.assertIsNotNone(inferred)
        self.assertEqual("Иоанн 14:1-3", inferred["parsed"]["ref"])
        passage = {
            "book": "Иоанн", "book_id": 43, "chapter": 14,
            "start_verse": 1, "end_chapter": 14, "end_verse": 3,
            "ref": "Иоанн 14:1-3", "open_ended": True,
        }
        slides = replay_smart_slide_state(inferred, "one_verse")
        self.assertTrue(extend_open_ended_replay_passage(
            passage,
            slides,
            SimpleNamespace(top_candidate=hit(
                "Ин. 14:3-4", 90.0, matched=("приготовлю",),
                book_id=43, chapter=14, verse=3, end_verse=4,
            )),
        ))
        self.assertEqual("Иоанн 14:1-4", passage["ref"])
        self.assertEqual([1, 2, 3, 4], [item["verse"] for item in slides["targets"]])

    def test_open_ended_replay_reading_waits_instead_of_completing(self) -> None:
        from tools.replay_audio_files import defer_open_ended_replay_completion

        decision = defer_open_ended_replay_completion(
            {"open_ended": True},
            {
                "action": "complete",
                "action_label": "закончить диапазон",
                "will_transition": False,
                "reason": "strong_final_boundary",
            },
        )

        self.assertEqual("keep", decision["action"])
        self.assertEqual("ожидать следующий стих", decision["action_label"])
        self.assertEqual("await_next_element_for_open_reading", decision["reason"])

    def test_replay_does_not_open_text_reading_after_a_long_gap(self) -> None:
        from tools.replay_audio_files import infer_replay_sequential_text_reading

        state = {}
        first = {"source": "text_citation", "parsed": {
            "book": "Иоанн", "chapter": 14, "start_verse": 1,
            "end_chapter": 14, "end_verse": 2,
        }}
        second = {"source": "text_citation", "parsed": {
            "book": "Иоанн", "chapter": 14, "start_verse": 2,
            "end_chapter": 14, "end_verse": 3,
        }}
        infer_replay_sequential_text_reading(first, state, 80.25)
        self.assertIsNone(infer_replay_sequential_text_reading(second, state, 101.0))

    def test_replay_smart_slide_shadow_waits_for_next_verse_after_current_boundary(self) -> None:
        from bible_parser_core.sequence_advancer import decide_sequence_advance
        from tools.replay_audio_files import (
            apply_replay_smart_slide_decision,
            replay_smart_slide_state,
        )

        state = replay_smart_slide_state({
            "parsed": {
                "book": "Иаков", "chapter": 1, "start_verse": 19,
                "end_chapter": 1, "end_verse": 27, "ref": "Иаков 1:19-27",
            },
        }, "one_verse")
        self.assertIsNotNone(state)
        self.assertEqual(9, len(state["targets"]))
        self.assertFalse(state["current_slide_visible"])

        activation = decide_sequence_advance(
            state,
            hit("Иак. 1:19", 80.0, matched=("слушать", "говорить", "гнев"),
                book_id=59, chapter=1, verse=19),
            accepted=True,
            reason="accepted",
            score=80.0,
            margin=20.0,
            matched_words=3,
        )
        self.assertEqual("activate", activation["action"])
        self.assertTrue(apply_replay_smart_slide_decision(state, activation))
        self.assertTrue(state["current_slide_visible"])

        decision = decide_sequence_advance(
            state,
            hit("Иак. 1:19", 80.0, matched=("слушать", "говорить", "гнев"),
                book_id=59, chapter=1, verse=19, ending_overlap_words=2),
            accepted=True,
            reason="accepted",
            score=80.0,
            margin=20.0,
            matched_words=3,
        )
        self.assertEqual("keep", decision["action"])
        self.assertTrue(apply_replay_smart_slide_decision(state, decision))
        self.assertEqual(0, state["current_index"])

    def test_manual_smart_slide_context_builds_one_verse_range(self) -> None:
        from tools.replay_audio_files import manual_smart_slide_state

        state = manual_smart_slide_state(
            "Колоссянам 3:5-7",
            bible_path=DEFAULT_BIBLE,
            slide_mode="one_verse",
        )

        self.assertEqual("Колоссянам 3:5-7", state["ref"])
        self.assertTrue(state["manual_context"])
        self.assertEqual([5, 6, 7], [target["verse"] for target in state["targets"]])

    def test_replay_smart_slide_prefers_weak_nearby_evidence_over_distant_global_hit(self) -> None:
        from tools.replay_audio_files import replay_smart_slide_decision

        state = {
            "book_id": 19,
            "current_index": 1,
            "targets": [
                {"start_chapter": 72, "start_verse": verse, "chapter": 72, "verse": verse}
                for verse in range(6, 13)
            ],
        }
        scoped = SimpleNamespace(
            accepted=False,
            reason="score_below_threshold",
            score=48.0,
            margin=15.0,
            matched_words=4,
            top_candidate=hit(
                "Пс. 72:8", 48.0, matched=("слово",) * 4,
                book_id=19, chapter=72, verse=8,
            ),
        )
        global_hit = SimpleNamespace(
            accepted=True,
            reason="immediate_strong_match",
            score=95.0,
            margin=40.0,
            matched_words=7,
            top_candidate=hit(
                "Пс. 72:11", 95.0, matched=("слово",) * 7,
                book_id=19, chapter=72, verse=11,
            ),
        )

        decision = replay_smart_slide_decision(state, global_hit, scoped)

        self.assertEqual("assisted_synchronize_forward", decision["action"])
        self.assertEqual(2, decision["target_index"])
        self.assertEqual("sequence_scoped", decision["evidence_source"])

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

    def test_useful_weak_text_match_becomes_operator_hint(self) -> None:
        from tools.vosk_grammar_probe import text_operator_hint_payload

        candidate = hit(
            "Флп. 2:3", 65.547,
            matched=("ничто", "тщеславие", "смиренномудрие", "почитать", "другой", "себя"),
            book_id=50,
            chapter=2,
            verse=3,
        )
        decision = SimpleNamespace(
            accepted=False,
            top_candidate=candidate,
            window_text="ничего не делай по тщеславию почитай один другого выше себя",
            score=65.547,
            margin=30.15,
            matched_words=6,
            confirmations=0,
            reason="score_below_threshold",
        )

        payload = text_operator_hint_payload(decision, decision.window_text)

        self.assertIsNotNone(payload)
        self.assertEqual("Филиппийцам 2:3", payload["slide"]["ref"])
        self.assertEqual("text_operator_hint", payload["slide"]["source"])
        self.assertEqual("weak_text_match", payload["slide"]["label"])

    def test_ambiguous_weak_text_match_does_not_interrupt_operator(self) -> None:
        from tools.vosk_grammar_probe import text_operator_hint_payload

        candidate = hit(
            "Флп. 2:3", 65.547,
            matched=("почитать", "другой", "себя"),
            book_id=50,
            chapter=2,
            verse=3,
        )
        decision = SimpleNamespace(
            accepted=False,
            top_candidate=candidate,
            window_text="почитай один другого выше себя",
            score=65.547,
            margin=5.0,
            matched_words=3,
            confirmations=0,
            reason="score_below_threshold",
        )

        self.assertIsNone(text_operator_hint_payload(decision, decision.window_text))

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

    def test_text_match_expands_an_announced_range_from_its_first_verse(self) -> None:
        from tools.vosk_grammar_probe import text_citation_payload

        candidate = hit(
            "Иак. 1:13", 94.0,
            matched=("искушение", "бог"),
            book_id=59, chapter=1, verse=13,
        )
        decision = SimpleNamespace(
            top_candidate=candidate,
            window_text="в искушении не говори бог меня искушает",
            score=94.0,
            margin=20.0,
            matched_words=2,
            confirmations=1,
            reason="immediate_strong_match",
        )

        payload = text_citation_payload(
            decision,
            "тринадцатый пятнадцатый стих в искушении не говори бог меня искушает",
        )

        self.assertEqual("Иаков 1:13-15", payload["parsed"]["ref"])
        self.assertTrue(payload["text_citation"]["announced_range_expanded"])
        self.assertIn("но каждый искушается", payload["slide"]["verse"])
        self.assertIn("похоть же, зачав", payload["slide"]["verse"])

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


class SmartSlideReviewTest(unittest.TestCase):
    def test_shadow_reviews_are_grouped_and_saved_separately(self) -> None:
        from tools.review_trigger_cases import (
            collect_smart_slide_entries,
            save_smart_slide_review,
            smart_slide_event_paths,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_one = root / "run_001"
            run_two = root / "run_002"
            run_one.mkdir()
            run_two.mkdir()
            (run_one / "session.json").write_text(
                json.dumps({"audio": "audio.wav", "continued_window_context": False}),
                encoding="utf-8",
            )
            (run_two / "session.json").write_text(
                json.dumps({"audio": "audio.wav", "continued_window_context": True}),
                encoding="utf-8",
            )
            shadow_one = {
                "event": "SMART_SLIDE_SHADOW",
                "passage": "Марк 1:21-34",
                "action": "advance",
                "current_index": 0,
                "target_index": 1,
                "replay_seconds": 12.5,
                "window": "текст двадцать второго стиха",
            }
            shadow_two = {
                "event": "SMART_SLIDE_SHADOW",
                "passage": "Марк 1:21-34",
                "action": "complete",
                "current_index": 1,
                "target_index": 2,
                "replay_seconds": 3.0,
                "window": "текст последнего стиха",
            }
            (run_one / "events.jsonl").write_text(
                json.dumps({"event": "ASR_FINAL"}) + "\n"
                + json.dumps(shadow_one, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (run_two / "events.jsonl").write_text(
                json.dumps(shadow_two, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (root / "latest_replay_batch.json").write_text(
                json.dumps({"runs": [str(run_one), str(run_two)]}),
                encoding="utf-8",
            )

            paths = smart_slide_event_paths(root, latest_batch=True)
            entries = collect_smart_slide_entries(paths)

            self.assertEqual(2, len(entries))
            self.assertEqual(entries[0].sequence_id, entries[1].sequence_id)
            self.assertEqual((1, 2), (entries[0].sequence_position, entries[0].sequence_total))
            self.assertEqual((2, 2), (entries[1].sequence_position, entries[1].sequence_total))

            entries[0].review = {
                "review_category": "correct_transition",
                "status": "reviewed",
            }
            save_smart_slide_review(entries[0])

            saved = json.loads((run_one / "smart_slide_reviews.jsonl").read_text(encoding="utf-8"))
            self.assertEqual("correct_transition", saved["review_category"])
            self.assertEqual(entries[0].event_id, saved["event_id"])
            self.assertFalse((run_one / "trigger_cases.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
