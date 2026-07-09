import unittest

from bible_parser_core.live_pipeline import LiveReferencePipeline, build_grammar


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

    def test_bare_book_fragment_can_start_short_numeric_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("лука")
        self.assertFalse(first.get("matched"))
        self.assertEqual(["лука"], first.get("vosk_buffer"))

        second = pipeline.process_text("четырнадцать двадцать восемь тридцать")
        self.assertEqual("Лука 14:28-30", second.get("parsed", {}).get("ref"))

    def test_spoken_first_corinthians_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое коринфянам вторая глава шестнадцатый стих")

        self.assertEqual("1 Коринфянам 2:16", result.get("parsed", {}).get("ref"))

    def test_short_first_john_with_single_n_asr_variant(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое иоана три два")

        self.assertEqual("1 Иоанна 3:2", result.get("parsed", {}).get("ref"))

        result = pipeline.process_text("первое иоана четыре восемнадцать")

        self.assertEqual("1 Иоанна 4:18", result.get("parsed", {}).get("ref"))

    def test_nonexistent_first_corinthians_verse_does_not_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое коринфянам вторая глава двадцать пятый стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("invalid_verse", result.get("invalid_reference", {}).get("reason"))
        self.assertEqual("1 Коринфянам 2:25", result.get("invalid_reference", {}).get("ref"))
        self.assertIn("Такого стиха нет", result.get("message", ""))

    def test_command_suffix_overrides_incomplete_epistle_prefix(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое послание к читаем бытие третья глава шестой стих")

        self.assertEqual("Бытие 3:6", result.get("parsed", {}).get("ref"))

    def test_complete_epistle_reference_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое послание петра третья глава шестой стих")

        self.assertEqual("1 Петра 3:6", result.get("parsed", {}).get("ref"))

    def test_gospel_without_book_name_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от пятнадцать тринадцать откройте")

        self.assertFalse(result.get("matched"))
        self.assertEqual("gospel_without_book_name", result.get("blocked_weak_context"))

    def test_gospel_with_book_name_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна пятнадцать тринадцать")

        self.assertEqual("Иоанн 15:13", result.get("parsed", {}).get("ref"))

    def test_prophet_book_chapter_without_verse_does_not_create_epistle_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание второе книга пророка иеремии восьмая глава")

        self.assertFalse(result.get("matched"))
        self.assertEqual("prophet_book_chapter_without_verse", result.get("blocked_weak_context"))

    def test_prophet_book_with_verse_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("книга пророка иеремии восьмая глава первый стих")

        self.assertEqual("Иеремия 8:1", result.get("parsed", {}).get("ref"))

    def test_vosk_grammar_contains_range_words_with_yo_forms(self):
        grammar = set(build_grammar())

        self.assertIn("по", grammar)
        self.assertIn("четвёртого", grammar)
        self.assertIn("четвёртая", grammar)

    def test_slow_split_deuteronomy_range_with_yo_form(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("из книги второзаконие двадцать седьмая глава", now_ms=1_000).get("matched")
        )
        result = pipeline.process_text("с двадцать четвёртого по двадцать шестой стих", now_ms=2_000)

        self.assertEqual("Второзаконие 27:24-26", result.get("parsed", {}).get("ref"))

    def test_noise_does_not_report_invalid_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("коринфянам просто параллельно")

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("invalid_reference"))

    def test_gospel_phrase_in_noisy_context_can_start_next_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("числа откроем евангелие от матфея")
        self.assertFalse(first.get("matched"))

        second = pipeline.process_text("восьмая глава первого пятые стих")
        self.assertEqual("Матфей 8:1-5", second.get("parsed", {}).get("ref"))

    def test_slow_split_reference_accumulates_inside_time_window(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("давайте откроем евангелие от матфея", now_ms=1_000).get("matched")
        )
        self.assertFalse(pipeline.process_text("восьмая глава", now_ms=2_100).get("matched"))
        third = pipeline.process_text("с первого", now_ms=3_000)
        self.assertFalse(third.get("matched"))
        self.assertEqual("incomplete_first_verse_after_chapter", third.get("blocked_weak_context"))
        self.assertTrue(third.get("buffer_kept_for_open_range"))

        fourth = pipeline.process_text("по пятый стих", now_ms=4_000)
        self.assertEqual("Матфей 8:1-5", fourth.get("parsed", {}).get("ref"))
        self.assertFalse(fourth.get("buffer_reset_by_gap"))

    def test_slow_split_epistle_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("первое послание ефесянам", now_ms=2_000).get("matched"))
        result = pipeline.process_text("вторая глава девятая десятая стих", now_ms=3_000)

        self.assertEqual("Ефесянам 2:9-10", result.get("parsed", {}).get("ref"))

    def test_slow_split_numbered_epistle_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем второе тимофею", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("вторая глава", now_ms=2_000).get("matched"))
        result = pipeline.process_text("девятнадцатый двадцать первое стих", now_ms=3_000)

        self.assertEqual("2 Тимофею 2:19-21", result.get("parsed", {}).get("ref"))

    def test_split_open_range_without_po_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("первое послание коринфянам третья глава", now_ms=1_000).get("matched")
        )
        result = pipeline.process_text("девятого двадцатую стих", now_ms=2_000)

        self.assertEqual("1 Коринфянам 3:9-20", result.get("parsed", {}).get("ref"))

    def test_ambiguous_timothy_without_number_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("тимофею третья глава четвёртого по пятой стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("ambiguous_numbered_timothy", result.get("blocked_weak_context"))

    def test_timothy_text_does_not_resolve_to_john(self):
        pipeline = LiveReferencePipeline()

        pipeline.process_text("первого послания тимофею", now_ms=1_000)
        pipeline.process_text("восьмую стих", now_ms=2_000)
        result = pipeline.process_text(
            "откройте послания тимофею первое тимофею пятую",
            now_ms=3_000,
        )

        self.assertFalse(result.get("matched"))
        self.assertEqual("resolver_conflicts_with_timothy", result.get("blocked_weak_context"))

    def test_explicit_numbered_timothy_still_works(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("первое тимофею третья глава четвёртого по пятой стих")
        second = pipeline.process_text("второе тимофею третья глава четвёртого по пятой стих")

        self.assertEqual("1 Тимофею 3:4-5", first.get("parsed", {}).get("ref"))
        self.assertEqual("2 Тимофею 3:4-5", second.get("parsed", {}).get("ref"))

    def test_numbered_epistle_with_poslanie_does_not_use_book_number_as_chapter(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("второе послание коринфянам пятого восемнадцатый стих")

        self.assertEqual("2 Коринфянам 5:18", result.get("parsed", {}).get("ref"))

    def test_numbered_corinthians_chapter_only_does_not_become_john(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первого послания коринфянам шестая глава")

        self.assertFalse(result.get("matched"))
        self.assertIsNone(result.get("parsed"))

    def test_split_reference_uses_asr_word_timestamps_for_buffer_gap(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text(
            "первого послания коринфянам шестая глава",
            now_ms=1_068_250,
            asr_result={
                "result": [
                    {"start": 1066.03, "end": 1066.27, "word": "первого"},
                    {"start": 1066.27, "end": 1066.66, "word": "послания"},
                    {"start": 1066.66, "end": 1067.11, "word": "коринфянам"},
                    {"start": 1067.11, "end": 1067.4279, "word": "шестая"},
                    {"start": 1067.44, "end": 1067.8, "word": "глава"},
                ],
                "text": "первого послания коринфянам шестая глава",
            },
        )
        self.assertFalse(first.get("matched"))

        result = pipeline.process_text(
            "девятнадцатый двадцатая стих",
            now_ms=1_071_250,
            asr_result={
                "result": [
                    {"start": 1069.36, "end": 1069.96, "word": "девятнадцатый"},
                    {"start": 1069.96, "end": 1070.44, "word": "двадцатая"},
                    {"start": 1070.44, "end": 1070.74, "word": "стих"},
                ],
                "text": "девятнадцатый двадцатая стих",
            },
        )

        self.assertEqual("1 Коринфянам 6:19-20", result.get("parsed", {}).get("ref"))
        self.assertEqual("asr_words", result.get("delta_source"))
        self.assertLess(result.get("delta_ms"), 2_000)

    def test_suspicious_feminine_first_stich_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("послание ефесянам третью", now_ms=1_000).get("matched"))
        result = pipeline.process_text("первую стих", now_ms=2_000)

        self.assertFalse(result.get("matched"))
        self.assertEqual("suspicious_first_verse_form", result.get("blocked_weak_context"))

    def test_incomplete_first_verse_after_chapter_waits_for_range(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("откроем первая яна вторая глава первого", now_ms=1_000)
        self.assertFalse(first.get("matched"))
        self.assertEqual("incomplete_first_verse_after_chapter", first.get("blocked_weak_context"))

        result = pipeline.process_text("по шестой стих", now_ms=2_000)
        self.assertEqual("1 Иоанна 2:1-6", result.get("parsed", {}).get("ref"))

    def test_compact_reference_without_markers_has_extra_risk(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "римлянам четвёртого шестнадцать",
            asr_result={
                "result": [
                    {"conf": 1.0, "start": 3538.08, "end": 3538.53, "word": "римлянам"},
                    {"conf": 0.809894, "start": 3538.53, "end": 3539.165215, "word": "четвёртого"},
                    {"conf": 0.642576, "start": 3539.19, "end": 3539.655645, "word": "шестнадцать"},
                ],
                "text": "римлянам четвёртого шестнадцать",
            },
        )

        self.assertEqual("Римлянам 4:16", result.get("parsed", {}).get("ref"))
        self.assertEqual("medium", result.get("risk_level"))
        self.assertIn("compact_reference_without_markers", result.get("risk_reasons"))

    def test_bare_numbers_first_verse_does_not_auto_match(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("числа первое первого стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_bare_numbers_first_verse", result.get("blocked_weak_context"))

        explicit = pipeline.process_text("книга числа первая глава первый стих")
        self.assertEqual("Числа 1:1", explicit.get("parsed", {}).get("ref"))

    def test_missing_chapter_word_after_ordinal_tens_does_not_merge_chapter_and_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("исайя сороковая первого девятый стих")

        self.assertEqual("Исаия 40:1-9", result.get("parsed", {}).get("ref"))

    def test_cardinal_tens_can_still_form_compound_chapter_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("исайя сорок первого девятый стих")

        self.assertEqual("Исаия 41:9", result.get("parsed", {}).get("ref"))

    def test_descending_repeated_verse_is_treated_as_speaker_correction(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие двадцать четвёртая глава пятьдесят вторую стих пятьдесят первое стих")

        self.assertEqual("Бытие 24:51", result.get("parsed", {}).get("ref"))

    def test_confusable_seventeen_eighteen_verse_adds_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("римлянам восьмая глава восемнадцатый стих")

        self.assertEqual("Римлянам 8:18", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Римлянам 8:17", refs)
        self.assertIn("confusable_number_alternative", result.get("risk_reasons"))

    def test_confusable_thirteen_thirty_chapter_adds_existing_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие тридцатая глава первый стих")

        self.assertEqual("Бытие 30:1", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Бытие 13:1", refs)

    def test_confusable_twelve_thirteen_verse_adds_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("римлянам восьмая глава тринадцатый стих")

        self.assertEqual("Римлянам 8:13", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Римлянам 8:12", refs)

    def test_confusable_twelve_thirteen_chapter_adds_existing_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("бытие тринадцатая глава первый стих")

        self.assertEqual("Бытие 13:1", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Бытие 12:1", refs)

    def test_unnumbered_corinthians_epistle_adds_colossians_alternative(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послания коринфянам первого глава девятнадцать два второе стих")

        self.assertEqual("2 Коринфянам 1:19-22", result.get("parsed", {}).get("ref"))
        refs = {item.get("ref") for item in result.get("ambiguous_alternatives") or []}
        self.assertIn("Колоссянам 1:19-22", refs)
        self.assertIn("confusable_book_alternative", result.get("risk_reasons"))

    def test_repeated_seventeen_or_eighteen_range_is_repaired(self):
        pipeline = LiveReferencePipeline()

        seventeen = pipeline.process_text("римлянам восьмая глава семнадцатый семнадцатый стих")
        eighteen = pipeline.process_text("римлянам восьмая глава восемнадцатый восемнадцатый стих")

        self.assertEqual("Римлянам 8:17-18", seventeen.get("parsed", {}).get("ref"))
        self.assertEqual("parser_repeated_confusable_range", seventeen.get("source"))
        self.assertEqual("Римлянам 8:17-18", eighteen.get("parsed", {}).get("ref"))
        self.assertEqual("parser_repeated_confusable_range", eighteen.get("source"))

    def test_repeated_psalm_references_are_returned_as_compact_list(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "псалом девятый девятнадцатый стих "
            "псалом тридцать восьмой восьмой стих "
            "псалом тридцать девять пятой стих "
            "псалом шестьдесят первый пятой стих "
            "псалом семидесятый пятой стих седьмой стих псалом"
        )

        self.assertTrue(result.get("matched"))
        self.assertIsNone(result.get("parsed"))
        self.assertEqual("parser_reference_list", result.get("source"))
        refs = [item.get("ref") for item in result.get("reference_list") or []]
        self.assertEqual(
            [
                "Псалтирь 9:19",
                "Псалтирь 38:8",
                "Псалтирь 39:5",
                "Псалтирь 61:5",
                "Псалтирь 70:5-7",
            ],
            refs,
        )

    def test_split_psalm_range_before_psalm_title_uses_full_buffer(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("первого по", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("четырнадцатый стих", now_ms=2_000).get("matched"))
        result = pipeline.process_text("псалом семьдесят второй", now_ms=3_000)

        self.assertEqual("Псалтирь 72:1-14", result.get("parsed", {}).get("ref"))

    def test_psalm_range_accepts_stih_misheard_as_seven_before_psalm_title(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("четвёртого по двенадцатые семь семьдесят второго псалмы")

        self.assertEqual("Псалтирь 72:4-12", result.get("parsed", {}).get("ref"))

    def test_psalm_without_stich_keeps_ordinal_tens_as_compound_psalm_number(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("псалом девяностый девять")

        self.assertEqual("Псалтирь 99:1-5", result.get("parsed", {}).get("ref"))

    def test_short_psalm_chapter_verse_without_stich_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("псалом двадцать два четыре")

        self.assertEqual("Псалтирь 22:4", result.get("parsed", {}).get("ref"))

    def test_numbered_general_epistle_with_poslanie_still_works(self):
        pipeline = LiveReferencePipeline()

        peter = pipeline.process_text("второе послание петра третья глава четвёртый стих")
        john = pipeline.process_text("первое послание иоанна вторая глава восьмой стих")

        self.assertEqual("2 Петра 3:4", peter.get("parsed", {}).get("ref"))
        self.assertEqual("1 Иоанна 2:8", john.get("parsed", {}).get("ref"))

    def test_split_two_digit_range_start_uses_end_tens(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие диана пятая глава третий три четвёртую стих")
        compact = pipeline.process_text("евангелие иоанна пятая глава третий тридцать четвёртый стих")
        twenties = pipeline.process_text("евангелие от иоанна пятая глава первый двадцать второй стих")

        self.assertEqual("Иоанн 5:33-34", result.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 5:33-34", compact.get("parsed", {}).get("ref"))
        self.assertEqual("Иоанн 5:21-22", twenties.get("parsed", {}).get("ref"))

    def test_short_single_digit_range_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("евангелие от иоанна пятая глава третий четвёртый стих")

        self.assertEqual("Иоанн 5:3-4", result.get("parsed", {}).get("ref"))

    def test_slow_split_old_testament_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("читаем из книги второзаконие", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("двадцать шестая глава", now_ms=2_000).get("matched"))
        self.assertFalse(pipeline.process_text("девятого", now_ms=3_000).get("matched"))
        result = pipeline.process_text("четырнадцатая стих", now_ms=4_000)

        self.assertEqual("Второзаконие 26:9-14", result.get("parsed", {}).get("ref"))

    def test_slow_split_genesis_reference_uses_explicit_context(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("откроем книга бытие", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("двадцать седьмую главы", now_ms=2_000).get("matched"))
        result = pipeline.process_text("с тридцатого тридцать четвёртая стих", now_ms=3_000)

        self.assertEqual("Бытие 27:30-34", result.get("parsed", {}).get("ref"))

    def test_split_reference_resets_after_long_pause(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(
            pipeline.process_text("давайте откроем евангелие от матфея", now_ms=1_000).get("matched")
        )

        second = pipeline.process_text("восьмая глава с первого по пятый стих", now_ms=4_500)
        self.assertFalse(second.get("matched"))
        self.assertTrue(second.get("buffer_reset_by_gap"))
        self.assertEqual(["восьмая глава с первого по пятый стих"], second.get("vosk_buffer"))

    def test_stale_buffer_does_not_repeat_previous_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("иоана три шестнадцать")
        self.assertEqual("Иоанн 3:16", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("мих от до с ины")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["мих от до с ины"], second.get("vosk_buffer"))

    def test_stale_buffer_does_not_cascade_false_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("два же второе десятую притч")
        self.assertEqual("Притчи 2:2-10", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("четвертая из")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["четвертая из"], second.get("vosk_buffer"))

    def test_short_moses_noise_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("десять три моисея")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_short_moses_context", result.get("blocked_weak_context"))

    def test_levit_range_after_short_moses_noise_still_works(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("десять три моисея", now_ms=1_000).get("matched"))
        self.assertFalse(pipeline.process_text("читаем", now_ms=2_000).get("matched"))
        self.assertFalse(pipeline.process_text("книга левит двадцать четвёртая глава", now_ms=3_000).get("matched"))
        result = pipeline.process_text("двадцатого по двадцать второе стих", now_ms=4_000)

        self.assertEqual("Левит 24:20-22", result.get("parsed", {}).get("ref"))

    def test_short_yana_noise_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("десять яна семь")

        self.assertFalse(result.get("matched"))
        self.assertEqual("weak_short_yana_context", result.get("blocked_weak_context"))

    def test_unknown_prefix_before_reversed_verse_context_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("[unk] шестой стих двадцать седьмой главы книги второзаконие")

        self.assertFalse(result.get("matched"))
        self.assertEqual("unknown_prefix_before_reversed_verse", result.get("blocked_weak_context"))

    def test_unknown_prefix_inside_book_chapter_context_still_works(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("из книги второзаконие двадцать седьмая глава [unk] двадцать шестой стих")

        self.assertEqual("Второзаконие 27:26", result.get("parsed", {}).get("ref"))

    def test_clean_reference_has_low_risk_score(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "евангелие от иоанна три шестнадцать",
            asr_result={
                "result": [
                    {"word": "евангелие", "start": 0.0, "end": 0.5, "conf": 1.0},
                    {"word": "от", "start": 0.5, "end": 0.7, "conf": 1.0},
                    {"word": "иоанна", "start": 0.7, "end": 1.1, "conf": 1.0},
                    {"word": "три", "start": 1.1, "end": 1.3, "conf": 1.0},
                    {"word": "шестнадцать", "start": 1.3, "end": 1.9, "conf": 1.0},
                ]
            },
        )

        self.assertEqual("Иоанн 3:16", result.get("parsed", {}).get("ref"))
        self.assertLess(result.get("risk_score"), 0.3)
        self.assertEqual("low", result.get("risk_level"))

    def test_distorted_fast_reference_has_high_risk_score(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "пророк данила один пятой стих",
            asr_result={
                "result": [
                    {"word": "пророк", "start": 0.0, "end": 0.15, "conf": 0.72},
                    {"word": "данила", "start": 0.16, "end": 0.31, "conf": 0.62},
                    {"word": "один", "start": 0.32, "end": 0.42, "conf": 0.58},
                    {"word": "пятой", "start": 0.43, "end": 0.54, "conf": 0.61},
                    {"word": "стих", "start": 0.55, "end": 0.68, "conf": 0.91},
                ]
            },
        )

        self.assertEqual("Даниил 1:5", result.get("parsed", {}).get("ref"))
        self.assertGreaterEqual(result.get("risk_score"), 0.6)
        self.assertEqual("high", result.get("risk_level"))

    def test_missing_twenty_before_range_end_is_restored(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого второе стих")

        self.assertEqual("Иаков 2:18-22", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))
        self.assertIn("missing_twenty_range_repair", result.get("risk_reasons"))

    def test_missing_twenty_before_range_end_can_override_wrong_chapter_parse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого третьего стих")

        self.assertEqual("Иаков 2:18-23", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))

    def test_missing_twenty_before_ninth_range_end_is_restored(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("деяния вторая глава восемнадцатого девятого стих")

        self.assertEqual("Деяния 2:18-29", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser_missing_twenty_range", result.get("source"))

    def test_missing_twenty_range_does_not_restore_nonexistent_end_verse(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого девятого стих")

        self.assertEqual("Иаков 2:18", result.get("parsed", {}).get("ref"))

    def test_missing_twenty_range_does_not_apply_to_tenth(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("послание яков вторая восемнадцатого десятого стих")

        self.assertEqual("Иаков 2:18", result.get("parsed", {}).get("ref"))
        self.assertEqual("parser", result.get("source"))

    def test_numbered_kingdoms_range_waits_for_chapter_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("первое книги царств с четвёртого по восьмой стих")

        self.assertFalse(result.get("matched"))
        self.assertEqual("numbered_kingdoms_range_without_chapter", result.get("blocked_weak_context"))

    def test_numbered_kingdoms_range_works_with_chapter_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text(
            "читаем из двадцать седьмой главы первое книги царств четвёртого по восьмой стих"
        )

        self.assertEqual("1 Царств 27:4-8", result.get("parsed", {}).get("ref"))

    def test_joshua_chapter_suffix_waits_for_verse_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("читаем из книга иисуса навина четырнадцатую из четырнадцатый главы")

        self.assertFalse(result.get("matched"))
        self.assertEqual("joshua_chapter_suffix_without_verse", result.get("blocked_weak_context"))

    def test_joshua_range_works_with_verse_context(self):
        pipeline = LiveReferencePipeline()

        result = pipeline.process_text("четырнадцатая глава книга иисуса навина двенадцатый четырнадцатая стих")

        self.assertEqual("Иисус Навин 14:12-14", result.get("parsed", {}).get("ref"))

    def test_noise_context_does_not_create_new_reference(self):
        pipeline = LiveReferencePipeline()

        first = pipeline.process_text("десятая притч девять числа")
        self.assertEqual("Числа 10:9", first.get("parsed", {}).get("ref"))

        second = pipeline.process_text("оны сто")
        self.assertFalse(second.get("matched"))
        self.assertEqual(["оны сто"], second.get("vosk_buffer"))

    def test_noise_context_does_not_create_daniel_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("оны сто").get("matched"))
        self.assertFalse(pipeline.process_text("данила к до").get("matched"))

        third = pipeline.process_text("восьмого")
        self.assertFalse(third.get("matched"))
        self.assertTrue(third.get("blocked_no_book_context"))

    def test_non_gospel_noise_suffix_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("с главы даниил").get("matched"))

        second = pipeline.process_text("шестого")
        self.assertFalse(second.get("matched"))

    def test_noisy_book_phrase_suffix_does_not_create_reference(self):
        pipeline = LiveReferencePipeline()

        self.assertFalse(pipeline.process_text("от шесть пророка ионы иакова книга амоса").get("matched"))

        second = pipeline.process_text("послание евр")
        self.assertFalse(second.get("matched"))


if __name__ == "__main__":
    unittest.main()
